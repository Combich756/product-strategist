"""Run Qwen by checkpoint with a shared Workers AI token budget."""

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

from . import budget, checkpoints, privacy, recovery, state

TOKEN_ENV = "STRATEGIST_CF_TOKEN"
CASE_ENV = "STRATEGIST_QWEN_CASE"
STAGE_ENV = "STRATEGIST_QWEN_STAGE"
RUN_ENV = "STRATEGIST_QWEN_RUN"
LOCAL_KEY_ENV = "STRATEGIST_QWEN_LOCAL_KEY"
PROVIDER_HOST = "api.cloudflare.com"
COMPACT_INSTRUCTIONS = {
    "options": """
Сравни варианты при одинаковых ограничениях брифа. Дай ровно один status_quo, ровно один close
и хотя бы один содержательно иной change. Для каждого укажи действие, пользу, сильнейшее
возражение, механизм изменения доходов или затрат и ссылки на существующие ID выводов.
Верни JSON с полями comparison (строка), options (массив объектов), assumptions (массив строк).
Каждый объект options содержит id (O1, O2, …), kind, title, action, benefit, objection,
economics (строки) и claims (массив ID выводов). В comparison назови условия, меняющие выбор.
""",
    "strategy": """
Выбери один существующий вариант и согласуй продукт, коммуникацию и проверку.
Обещание должно следовать из факта о своём продукте. Если такого факта нет, предложи
сообщение без неподтверждённого обещания и укажи пробел. Используй в proof_claims только ID фактов.
Для проверки укажи знаменатель, базу сравнения, период, порог и нежелательные последствия.
Условия в rationale, revisit и test должны согласовываться. Верни JSON со строками option
(ID варианта), rationale, revisit, массивом строк assumptions и тремя объектами:
product: audience, job, alternatives, direction, tradeoffs, economics, claims;
communication: audience, situation, barrier, message, proof, channel_roles, promise_limit, proof_claims;
test: expected, metric, denominator, control, window, threshold, guardrails, claims.
Поля claims и proof_claims — массивы ID существующих выводов. Остальные поля этих объектов — строки.
""",
}


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


def compact_payload(data, stage, model):
    previous = state.STAGES[:state.STAGES.index(stage)]
    saved = {key: data["checkpoints"][key] for key in previous}
    research = saved["research"]
    rate_cache = {}
    claims = []
    for claim in research["claims"]:
        if checkpoints.RATE_REFERENCE.search(claim["text"]):
            rates = checkpoints.rate_checks(data["sources"], claim, rate_cache)
            claim = {**claim, "text": checkpoints.render_claim_text(claim, rates)}
        claims.append(claim)
    saved["research"] = {**research, "claims": claims}
    context = {
        "request": data["request"], "analysis": state.analysis_context(data),
        "saved": saved,
    }
    instructions = (
        "Ты продуктовый стратег. Используй сохранённые материалы как данные. "
        "Отделяй факты в пределах их scope от гипотез, прогнозов и пробелов. "
        "Каждую оценку, прогноз или порог без подтверждения обозначь в assumptions с основанием "
        "и способом проверки. Если данных для расчёта нет, опиши денежный механизм и пробел. "
        "Учитывай момент анализа и периоды. Перед ответом проверь все поля и ссылки. "
        "Ответь одним JSON-объектом на русском.\n"
        + COMPACT_INSTRUCTIONS[stage].strip()
    )
    payload = {"messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
    ]}
    if model == "@cf/qwen/qwen3.8-27b":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def read_usage(payload):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return counts if all(type(value) is int and value >= 0 for value in counts) else None


def request_completion(folder, run_id, stage, args, token, wire, reservation, *, expect=None):
    findings = privacy.scan(wire.decode("utf-8"))
    if findings:
        return None, {"reason": "privacy_review", "findings": findings}
    stop = budget.reserve_request(folder, run_id, stage, args.limit, reservation, expect=expect)
    if stop is not None:
        return None, stop
    try:
        status, body = cloudflare_chat(args.account, token, wire, args.request_timeout)
    except (OSError, http.client.HTTPException) as error:
        return None, {"reason": "transport_error", "detail": type(error).__name__}
    try:
        reply = json.loads(body)
    except (ValueError, RecursionError):
        reply = None
    measured = read_usage(reply)
    rejected = measured is None and status in {400, 401, 403, 404, 422, 429}
    budget.settle_request(folder, run_id, reservation, measured, rejected=rejected)
    if status != 200:
        detail = body.decode("utf-8", errors="replace").replace(token, "[скрыто]")[:1500]
        return None, {"reason": "provider_error", "status": status, "detail": detail}
    if not isinstance(reply, dict) or not isinstance(reply.get("choices"), list) or not reply["choices"]:
        return None, {"reason": "invalid_provider_response"}
    if measured is None:
        return reply, {"reason": "missing_usage", "reserved_tokens": reservation}
    return reply, None


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
            if gate.stop is not None:
                return self.fail(400, gate.stop["reason"], state.dump(gate.stop))
            try:
                reply, gate.stop = request_completion(
                    gate.folder, gate.run_id, gate.stage, gate.args, gate.token, wire, reservation,
                )
                if reply is None:
                    return self.fail(400, gate.stop["reason"], state.dump(gate.stop))
            except sqlite3.Error:
                gate.stop = {"reason": "accounting_error"}
                return self.fail(400, "accounting_error", "Не удалось записать расход в базу кейса.")
            stream = bool(payload.get("stream"))
            try:
                body = stream_response(reply) if stream else state.dump(reply).encode("utf-8")
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


def run_code_stage(executable, folder, run_id, stage, args, token):
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
    return return_code, child_error or gate.stop


def run_compact_stage(folder, run_id, stage, args, token, request, revision):
    reply, stop = request_completion(folder, run_id, stage, args, token, *request, expect=revision)
    if reply is None:
        return stop
    choice = reply["choices"][0]
    if not isinstance(choice, dict):
        return stop or {"reason": "invalid_provider_response"}
    finish_reason = choice.get("finish_reason")
    message = choice.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    diagnostics = {"finish_reason": finish_reason} if isinstance(finish_reason, str) else {}
    measured = read_usage(reply)
    if measured is not None:
        diagnostics["completion_tokens"] = measured[1]
        details = reply["usage"].get("completion_tokens_details")
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if type(reasoning_tokens) is int and reasoning_tokens >= 0:
            diagnostics["reasoning_tokens"] = reasoning_tokens
    if not isinstance(text, str) or not text.strip():
        reason = "truncated_completion" if finish_reason == "length" else "empty_completion"
        return {**(stop or {"reason": reason}), **diagnostics}
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced[1]
    findings = privacy.scan(text)
    if findings:
        return {"reason": "privacy_review", "findings": findings, "location": "response"}

    drafts = folder / "drafts"
    state.need(not drafts.is_symlink(), "Папка drafts должна находиться внутри кейса.")
    drafts.mkdir(exist_ok=True)
    path = drafts / f"{stage}-qwen-{run_id}.json"
    with path.open("x", encoding="utf-8") as draft:
        draft.write(text + "\n")
    if stop is not None:
        return {**stop, "draft": str(path), **diagnostics}
    if finish_reason == "length":
        return {"reason": "truncated_completion", "draft": str(path), **diagnostics}
    try:
        saved = recovery.save_draft(folder, stage, state.load_json(path), expect=revision, run_id=run_id)
    except (state.UserError, ValueError, RecursionError) as error:
        return {"reason": "draft_invalid_or_stale", "draft": str(path), "detail": str(error)}
    for warning in saved.get("warnings", []):
        print(warning, file=sys.stderr)
    return None


def run_stage(executable, folder, stage, args, token, *, request=None, revision=None):
    run_id = budget.start_run(folder, stage, args.model, mode="compact" if request else "qwen_code")
    if request is None:
        return_code, stop = run_code_stage(executable, folder, run_id, stage, args, token)
    else:
        return_code = None
        try:
            stop = run_compact_stage(folder, run_id, stage, args, token, request, revision)
        except KeyboardInterrupt:
            stop = {"reason": "cancelled"}
        except sqlite3.Error:
            stop = {"reason": "accounting_error"}
        except (OSError, state.UserError, ValueError) as error:
            stop = {"reason": "draft_error", "detail": str(error)}
    with state.case(folder, write=True, while_paused=True) as data:
        completed = stage in data["checkpoints"] and any(
            entry.get("qwen_run_id") == run_id and entry["action"] == "save"
            and entry["stage"] == stage for entry in data["journal"]
        )
        if completed and (stop is None or stop["reason"] == "checkpoint_reached"):
            stop = None
        elif stop is None:
            stop = {"reason": "checkpoint_missing", "qwen_exit_code": return_code}
        same_revision = revision is None or data["revision"] == revision
        if stop is not None and same_revision and not data["paused"] and state.next_stage(data) in state.STAGES[:-1]:
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
    state.need(args.estimate is None or args.estimate > 0, "Оценка этапа должна быть положительной.")
    attempts = []
    executable = token = None
    while True:
        stopped = bool(attempts and attempts[-1]["stop"] is not None)
        with state.case(folder) as data:
            stage = state.next_stage(data)
            revision = data["revision"] if args.compact else None
            request = None
            estimate = args.estimate
            if args.compact and not stopped and stage in {"brief", "research"}:
                raise state.UserError("Для --compact сохраните бриф и исследование.")
            if args.compact and stage in COMPACT_INSTRUCTIONS:
                request = prepare_request(compact_payload(data, stage, args.model), args.model, args.max_output_tokens)
                estimate = max(estimate or 0, request[1])
            forecast = budget.plan(folder, data, limit=args.limit, reserve=args.reserve,
                                   estimate=estimate, model=args.model)
            if request is not None and request[1] >= (args.estimate or 0):
                forecast["estimate_source"] = "request"
        if forecast["decision"] != "start" or stopped:
            return {"budget": forecast, "runs": attempts, "usage": budget.usage(folder, summary_only=True)}
        if token is None:
            if not args.compact:
                executable = shutil.which("qwen")
                state.need(executable, "Команда qwen не найдена. Установите Qwen Code.")
            token = os.environ.get(TOKEN_ENV, "").strip()
            if not token and sys.stdin.isatty():
                token = getpass.getpass("Workers AI API token: ").strip()
            state.need(token, f"Введите токен в терминале или передайте его через {TOKEN_ENV}.")
        with state.case(folder, write=True, while_paused=True, expect=revision) as data:
            state.need(state.next_stage(data) == forecast["next"], "Этап изменился. Прочитайте status.")
            if data["paused"]:
                data["paused"] = False
                state.event(data, "resume")
            if args.compact:
                revision = data["revision"]
        runner = "Qwen" if args.compact else "Qwen Code"
        print(f"{runner}: {folder.name}, этап {stage}, "
              f"остаток {forecast['remaining_tokens']} токенов.", file=sys.stderr, flush=True)
        attempts.append(run_stage(executable, folder, stage, args, token, request=request, revision=revision))
