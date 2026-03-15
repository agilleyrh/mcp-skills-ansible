"""Proxy for Llama Stack Responses API compatibility with goose.
Fixes two issues:
  1. Strips max_output_tokens from requests (Llama Stack rejects it)
  2. Injects sequence_number into SSE events missing it (goose requires it)
"""
import json
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from starlette.routing import Route

UPSTREAM = "https://llamastack-llamastack.apps.ocp.v7hjl.sandbox2288.opentlc.com"
CLIENT = httpx.AsyncClient(verify=False, timeout=120.0)

STRIP_REQUEST_FIELDS = {"max_output_tokens", "max_completion_tokens"}


def fix_sse_event(data_str):
    """Fix SSE events for goose compatibility.
    - Adds sequence_number where missing (goose requires it on all events)
    - Skips reasoning_text content parts (goose ContentPart enum doesn't have this variant)
    """
    if not data_str.strip() or data_str.strip() == "[DONE]":
        return data_str
    try:
        obj = json.loads(data_str)
        if not isinstance(obj, dict):
            return data_str

        if "sequence_number" not in obj:
            obj["sequence_number"] = 0

        event_type = obj.get("type", "")

        # goose can't parse reasoning_text as a ContentPart variant
        if event_type in ("response.content_part.added", "response.content_part.done"):
            part = obj.get("part", {})
            if isinstance(part, dict) and part.get("type") == "reasoning_text":
                return None

        # Llama Stack nests error inside response object; goose expects top-level error
        if event_type == "response.failed":
            resp_obj = obj.get("response", {})
            if "error" not in obj and "error" in resp_obj:
                obj["error"] = resp_obj["error"]

        return json.dumps(obj)
    except (json.JSONDecodeError, TypeError):
        return data_str


async def proxy(request: Request):
    path = request.path_params["path"]
    body = await request.body()
    hdrs = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    url = f"{UPSTREAM}/v1/{path}"

    if request.method == "POST" and body:
        try:
            payload = json.loads(body)
            for field in STRIP_REQUEST_FIELDS:
                payload.pop(field, None)
            body = json.dumps(payload).encode()
        except (json.JSONDecodeError, AttributeError):
            payload = {}

        if isinstance(payload, dict) and payload.get("stream"):
            req = CLIENT.build_request("POST", url, content=body, headers=hdrs)
            resp = await CLIENT.send(req, stream=True)

            async def gen():
                try:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            fixed = fix_sse_event(line[6:])
                            if fixed is not None:
                                yield f"data: {fixed}\n\n"
                        elif line.strip():
                            yield f"{line}\n\n"
                finally:
                    await resp.aclose()

            return StreamingResponse(gen(), media_type="text/event-stream")
        else:
            resp = await CLIENT.post(url, content=body, headers=hdrs)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)

    resp = await CLIENT.request(request.method, url, content=body, headers=hdrs)
    try:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception:
        return JSONResponse(content={"raw": resp.text}, status_code=resp.status_code)


app = Starlette(
    routes=[
        Route(
            "/v1/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )
    ]
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9090)
