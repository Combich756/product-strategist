"""Run Qwen Code by checkpoint through a local Workers AI token gate."""

import getpass
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread

from . import budget, state

TOKEN_ENV = "STRATEGIST_CF_TOKEN"
CASE_ENV = "STRATEGIST_QWEN_CASE"
STAGE_ENV = "STRATEGIST_QWEN_STAGE"
RUN_ENV = "STRATEGIST_QWEN_RUN"
LOCAL_KEY_ENV = "STRATEGIST_QWEN_LOCAL_KEY"
PROVIDER_HOST = "api.cloudflare.com"


def cloudflare_chat(account, token, body, timeout):
    conn = http.client.HTTPSConnection(PROVIDER_HOST, timeout=timeout)
    try:
        conn.request(
            "POST", f"/client/v4/accounts/{account}/ai/v1/chat/completions", body,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def prepare_request(payload, model, max_output):
    state.need(isinstance(payload, dict) and isinstance(payload.get("messages"), list),
               "Ожидается запрос Chat Completions с messages.")
    count = payload.get("n", 1)
    state.need(type(count) is int and count == 1, "Поддерживается один вариант ответа модели.")
    for message in payload["messages"]:
        state.need(isinstance(message, dict), "Некорректное сообщение модели.")
        content = message.get("content")
        state.need(content is None or isinstance(content, str) or (
            isinstance(content, list) and all(
                isinstance(part, dict) and part.get("type") == "text" for part in content
            )
        ), "Токенный запуск поддерживает текстовые сообщения.")
    limits = [payload[key] for key in ("max_completion_tokens", "max_tokens") if payload.get(key) is not None]
    state.need(all(type(value) is int and value > 0 for value in limits), "Некорректный лимит ответа модели.")
    output = min(max_output, *limits) if limits else max_output
    wire = {**payload, "model": model, "stream": False, "max_completion_tokens": output}
    wire.pop("max_tokens", None)
    wire.pop("stream_options", None)
    encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    # Bytes of serialized text give a conservative estimate for the input.
    # The additional allowance covers the provider's message formatting.
    return encoded, len(encoded) + 1024 + 16 * len(payload["messages"]) + output


def read_usage(payload):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return counts if all(type(value) is int and value >= 0 for value in counts) else None


def stream_response(payload):
    """Expose a metered, buffered completion in the format Qwen's stream expects."""
    prefix = {key: payload[key] for key in ("id", "created", "model", "system_fingerprint", "service_tier")
              if key in payload}
    prefix["object"] = "chat.completion.chunk"
    chunks = []
    for choice in payload["choices"]:
        message = dict(choice["message"])
        if message.get("tool_calls"):
            message["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
        chunks.append({**prefix, "choices": [{"index": choice.get("index", 0), "delta": message,
                                             "finish_reason": None}]})
        chunks.append({**prefix, "choices": [{"index": choice.get("index", 0), "delta": {},
                                             "finish_reason": choice["finish_reason"]}]})
    if read_usage(payload) is not None:
        chunks.append({**prefix, "choices": [], "usage": payload["usage"]})
    return ("".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode("utf-8")


class CompletionHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, status, body, content_type="application/json"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def fail(self, status, reason, message):
        self.send_body(status, state.dump({"error": {"code": reason, "message": message}}).encode("utf-8"))

    def do_GET(self):
        if self.path != "/v1/models":
            return self.fail(404, "route", "Маршрут не найден.")
        if self.headers.get("Authorization") != f"Bearer {self.server.key}":
            return self.fail(401, "local_auth", "Требуется ключ локального запуска.")
        body = {"object": "list", "data": [{"id": self.server.alias, "object": "model"}]}
        self.send_body(200, state.dump(body).encode("utf-8"))

    def do_POST(self):
        gate = self.server
        if self.path != "/v1/chat/completions":
            return self.fail(404, "route", "Маршрут не найден.")
        if self.headers.get("Authorization") != f"Bearer {gate.key}":
            return self.fail(401, "local_auth", "Требуется ключ локального запуска.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            state.need(0 < size <= 2_000_000, "Размер запроса должен быть от 1 байта до 2 МБ.")
            payload = json.loads(self.rfile.read(size))
            wire, reservation = prepare_request(payload, gate.args.model, gate.args.max_output_tokens)
        except (state.UserError, ValueError, RecursionError) as error:
            return self.fail(400, "request_format", str(error))
        with gate.lock:
            try:
                if gate.stop is None:
                    gate.stop = budget.reserve_request(gate.folder, gate.run_id, gate.stage,
                                                       gate.args.limit, reservation)
                if gate.stop is not None:
                    return self.fail(400, gate.stop["reason"], state.dump(gate.stop))
                self.forward(wire, reservation, bool(payload.get("stream")))
            except sqlite3.Error:
                gate.stop = {"reason": "accounting_error"}
                self.fail(400, "accounting_error", "Не удалось записать расход в базу кейса.")

    def forward(self, wire, reservation, stream):
        gate = self.server
        try:
            status, body = cloudflare_chat(gate.args.account, gate.token, wire, gate.args.request_timeout)
        except (OSError, http.client.HTTPException) as error:
            gate.stop = {"reason": "transport_error", "detail": type(error).__name__}
            return self.fail(400, "transport_error", "Сбой связи с Cloudflare. Резерв оставлен до сверки.")
        try:
            reply = json.loads(body)
        except (ValueError, RecursionError):
            reply = None
        measured = read_usage(reply)
        rejected = measured is None and status in {400, 401, 403, 404, 422, 429}
        budget.settle_request(gate.folder, gate.run_id, reservation, measured, rejected=rejected)
        if status != 200:
            detail = body.decode("utf-8", errors="replace").replace(gate.token, "[скрыто]")[:1500]
            gate.stop = {"reason": "provider_error", "status": status, "detail": detail}
            return self.fail(400, "provider_error", f"Cloudflare HTTP {status}. {detail}")
        if not isinstance(reply, dict) or not reply.get("choices"):
            gate.stop = {"reason": "invalid_provider_response"}
            return self.fail(400, "invalid_provider_response", "В ответе Cloudflare отсутствует choices.")
        if measured is None:
            gate.stop = {"reason": "missing_usage", "reserved_tokens": reservation}
        if stream:
            try:
                body = stream_response(reply)
            except (KeyError, TypeError, ValueError):
                gate.stop = {"reason": "invalid_provider_response"}
                return self.fail(400, "invalid_provider_response", "Некорректный ответ Cloudflare.")
        self.send_body(200, body, "text/event-stream" if stream else "application/json")


class Gateway(ThreadingHTTPServer):
    def __init__(self, folder, run_id, stage, args, token):
        super().__init__(("127.0.0.1", 0), CompletionHandler)
        self.folder, self.run_id, self.stage = folder, run_id, stage
        self.args, self.token = args, token
        self.key = secrets.token_urlsafe(24)
        self.alias = "strategist-" + run_id
        self.base_url = f"http://127.0.0.1:{self.server_port}/v1"
        self.lock = Lock()
        self.stop = None


def invoke_qwen(executable, folder, gate):
    drafts = (folder / "drafts").as_posix().lstrip("/")
    settings = {
        "modelProviders": {"openai": [{
            "id": gate.alias, "name": gate.args.model, "baseUrl": gate.base_url, "envKey": LOCAL_KEY_ENV,
            "generationConfig": {"maxRetries": 0, "timeout": 1000 * (gate.args.request_timeout + 5)},
        }]},
        "permissions": {"allow": [
            "Bash(python3 -m strategist *)", f"Write(//{drafts}/**)", f"Edit(//{drafts}/**)",
        ]},
    }
    with TemporaryDirectory(prefix="strategist-qwen-") as home:
        (Path(home) / "settings.json").write_text(state.dump(settings), encoding="utf-8")
        env = {
            **os.environ, "QWEN_HOME": home, "QWEN_RUNTIME_DIR": home,
            "OPENAI_API_KEY": gate.key, LOCAL_KEY_ENV: gate.key,
            CASE_ENV: folder.name, STAGE_ENV: gate.stage, RUN_ENV: gate.run_id,
        }
        env.pop(TOKEN_ENV, None)
        local_hosts = ",".join(filter(None, (env.get("NO_PROXY"), "127.0.0.1", "localhost")))
        env.update(NO_PROXY=local_hosts, no_proxy=local_hosts)
        cli = f"python3 -m strategist --root {shlex.quote(str(folder.parent))}"
        prompt = (
            f"Прочитай AGENTS.md. Работай с кейсом {folder.name}. "
            f"Начни с {cli} context {folder.name} --compact. "
            f"Выполни только этап {gate.stage}, используй материалы кейса и разрешённые источники. "
            f"Проверь и сохрани этап через CLI {cli} по актуальной ревизии. "
            "Закончи работу сразу после сохранения."
        )
        process = subprocess.run(
            [executable, "--auth-type", "openai", "--model", gate.alias,
             "--openai-base-url", gate.base_url, "--approval-mode", "default", "-p", prompt],
            cwd=Path(__file__).resolve().parents[1], env=env, timeout=gate.args.wall_time,
            stdout=sys.stderr, stderr=sys.stderr,
        )
    return process.returncode


def run_stage(executable, folder, stage, args, token):
    run_id = budget.start_run(folder, stage, args.model)
    child_error = None
    return_code = None
    with Gateway(folder, run_id, stage, args, token) as gate:
        thread = Thread(target=gate.serve_forever, daemon=True)
        thread.start()
        try:
            return_code = invoke_qwen(executable, folder, gate)
        except subprocess.TimeoutExpired:
            child_error = {"reason": "wall_time", "seconds": args.wall_time}
        except KeyboardInterrupt:
            child_error = {"reason": "cancelled"}
        except OSError as error:
            child_error = {"reason": "launch_error", "detail": type(error).__name__}
        finally:
            gate.shutdown()
            thread.join()
    stop = child_error or gate.stop
    with state.case(folder, write=True, while_paused=True) as data:
        completed = stage in data["checkpoints"] and any(
            entry.get("qwen_run_id") == run_id and entry["action"] == "save"
            and entry["stage"] == stage for entry in data["journal"]
        )
        if completed and (stop is None or stop["reason"] == "checkpoint_reached"):
            stop = None
        elif stop is None:
            stop = {"reason": "checkpoint_missing", "qwen_exit_code": return_code}
        if stop is not None and not data["paused"] and state.next_stage(data) in state.STAGES[:-1]:
            data["paused"] = True
            state.event(data, "pause", reason=stop["reason"])
    outcome = "checkpoint_saved" if completed else stop["reason"]
    budget.finish_run(folder, run_id, outcome)
    return {"id": run_id, "stage": stage, "outcome": outcome, "qwen_exit_code": return_code, "stop": stop}


def run(folder, args):
    state.need(re.fullmatch(r"[a-fA-F0-9]{32}", args.account), "Нужен 32-символьный Cloudflare Account ID.")
    state.need(args.model.startswith("@cf/"), "Укажите модель Workers AI с префиксом @cf/.")
    state.need(min(args.max_output_tokens, args.request_timeout, args.wall_time) > 0,
               "Лимиты вывода и времени должны быть положительными.")
    attempts = []
    executable = token = None
    while True:
        with state.case(folder) as data:
            forecast = budget.plan(folder, data, limit=args.limit, reserve=args.reserve,
                                   estimate=args.estimate, model=args.model)
        if forecast["decision"] != "start" or (attempts and attempts[-1]["stop"] is not None):
            return {"budget": forecast, "runs": attempts, "usage": budget.usage(folder, summary_only=True)}
        if executable is None:
            executable = shutil.which("qwen")
            state.need(executable, "Команда qwen не найдена. Установите Qwen Code.")
            token = os.environ.get(TOKEN_ENV, "").strip()
            if not token and sys.stdin.isatty():
                token = getpass.getpass("Workers AI API token: ").strip()
            state.need(token, f"Введите токен в терминале или передайте его через {TOKEN_ENV}.")
        with state.case(folder, write=True, while_paused=True) as data:
            state.need(state.next_stage(data) == forecast["next"], "Этап изменился. Прочитайте status.")
            if data["paused"]:
                data["paused"] = False
                state.event(data, "resume")
        print(f"Qwen Code: {folder.name}, этап {forecast['next']}, "
              f"остаток {forecast['remaining_tokens']} токенов.", file=sys.stderr, flush=True)
        attempts.append(run_stage(executable, folder, forecast["next"], args, token))
