'''devspaces_mcp.py
FastMCP server that wraps OpenShift DevWorkspace API.

Run the server:
    python devspaces_mcp.py
    # defaults to stdio transport; for HTTP transport use:
    # python devspaces_mcp.py --port 8000
'''

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_SERVER = "https://api.ocp.v7hjl.sandbox2288.opentlc.com:6443"
DEVWORKSPACES_BASE = f"{API_SERVER}/apis/workspace.devfile.io/v1alpha2"
DW_TEMPLATES_PATH = "devworkspacetemplates"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _auth_headers(token: str) -> Dict[str, str]:
    """Return the Authorization header for a bearer token."""
    return {"Authorization": f"Bearer {token}"}

async def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Make an HTTP request with proper defaults and error handling.

    Args:
        method: HTTP verb ("GET", "POST", "PATCH", "DELETE").
        url: Full URL.
        token: Bearer token for authentication.
        json_body: JSON payload for POST/PATCH.
        headers: Additional headers.
        timeout: Seconds before the request times out.

    Returns:
        httpx.Response object. Raises a descriptive dict on failure.
    """
    request_headers = _auth_headers(token)
    if headers:
        request_headers.update(headers)
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        try:
            response = await client.request(
                method,
                url,
                headers=request_headers,
                json=json_body,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            # Return a unified error dict for the MCP tools.
            raise Exception(
                json.dumps(
                    {
                        "error": "http_error",
                        "status_code": exc.response.status_code,
                        "detail": exc.response.text,
                    }
                )
            )
        except Exception as exc:
            raise Exception(json.dumps({"error": "request_failed", "detail": str(exc)}))

# ---------------------------------------------------------------------------
# FastMCP server definition
# ---------------------------------------------------------------------------
mcp = FastMCP("DevSpaces MCP Server")

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
@mcp.tool
async def list_workspaces(namespace: str, token: str) -> List[Dict[str, Any]]:
    """List DevWorkspaces in a given namespace.

    Returns a list of workspace summary dictionaries.
    """
    url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces"
    resp = await _request("GET", url, token)
    data = resp.json()
    return data.get("items", [])

@mcp.tool
async def get_workspace(namespace: str, name: str, token: str) -> Dict[str, Any]:
    """Retrieve a single DevWorkspace by name.

    Returns the full workspace object (metadata, spec, status).
    """
    url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces/{name}"
    resp = await _request("GET", url, token)
    return resp.json()

@mcp.tool
async def delete_workspace(namespace: str, name: str, token: str) -> Dict[str, Any]:
    """Delete a DevWorkspace.

    Returns the Kubernetes delete response.
    """
    url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces/{name}"
    resp = await _request("DELETE", url, token)
    return resp.json()

@mcp.tool
async def start_workspace(namespace: str, name: str, token: str) -> Dict[str, Any]:
    """Start (or resume) a DevWorkspace.

    Sends a merge‑patch with ``{"spec": {"started": true}}``.
    """
    url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces/{name}"
    patch_body = {"spec": {"started": True}}
    resp = await _request(
        "PATCH",
        url,
        token,
        json_body=patch_body,
        headers={"Content-Type": "application/merge-patch+json"},
    )
    return resp.json()

@mcp.tool
async def stop_workspace(namespace: str, name: str, token: str) -> Dict[str, Any]:
    """Stop a running DevWorkspace.

    Sends a merge‑patch with ``{"spec": {"started": false}}``.
    """
    url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces/{name}"
    patch_body = {"spec": {"started": False}}
    resp = await _request(
        "PATCH",
        url,
        token,
        json_body=patch_body,
        headers={"Content-Type": "application/merge-patch+json"},
    )
    return resp.json()

@mcp.tool
async def create_workspace(
    namespace: str,
    ws_name: str,
    git_repo_url: str,
    token: str,
) -> Dict[str, Any]:
    """Create a DevWorkspace (IDE template + workspace).

    The function performs two steps:
    1. Create a ``DevWorkspaceTemplate`` named ``{ws_name}-ide``.
    2. After a short pause, create the ``DevWorkspace`` that references the template.

    Returns the created DevWorkspace object or an error dict.
    """
    # -------------------------------------------------------------------
    # 1. Create DevWorkspaceTemplate
    # -------------------------------------------------------------------
    template_name = f"{ws_name}-ide"
    template_payload = {
        "apiVersion": "workspace.devfile.io/v1alpha2",
        "kind": "DevWorkspaceTemplate",
        "metadata": {"name": template_name, "namespace": namespace},
        "spec": {
            "components": [
                {
                    "name": "che-code-runtime",
                    "container": {
                        "image": "quay.io/che-incubator/che-code:latest",
                        "memoryLimit": "2Gi",
                        "cpuLimit": "1000m",
                        "endpoints": [
                            {
                                "name": "che-code",
                                "exposure": "public",
                                "targetPort": 3100,
                                "protocol": "https",
                                "attributes": {
                                    "type": "main",
                                    "cookiesAuthEnabled": True,
                                    "discoverable": False,
                                    "urlRewriteSupported": True,
                                },
                            }
                        ],
                        "volumeMounts": [{"name": "checode", "path": "/checode"}],
                    },
                },
                {"name": "checode", "volume": {}},
            ]
        },
    }
    tmpl_url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/{DW_TEMPLATES_PATH}"
    await _request("POST", tmpl_url, token, json_body=template_payload)

    # -------------------------------------------------------------------
    # 2. Wait for the template to be ready (simple sleep).
    # -------------------------------------------------------------------
    await asyncio.sleep(2)

    # -------------------------------------------------------------------
    # 3. Create DevWorkspace referencing the template
    # -------------------------------------------------------------------
    workspace_payload = {
        "apiVersion": "workspace.devfile.io/v1alpha2",
        "kind": "DevWorkspace",
        "metadata": {"name": ws_name, "namespace": namespace},
        "spec": {
            "routingClass": "che",
            "started": True,
            "contributions": [{"name": "editor", "kubernetes": {"name": template_name}}],
            "template": {
                "projects": [
                    {
                        "name": "project",
                        "git": {"remotes": {"origin": git_repo_url}},
                    }
                ],
                "components": [
                    {
                        "name": "tools",
                        "container": {
                            "image": "quay.io/devfile/universal-developer-image:ubi8-latest",
                            "memoryLimit": "4Gi",
                            "cpuLimit": "2000m",
                            "mountSources": True,
                        },
                    }
                ],
            },
        },
    }
    ws_url = f"{DEVWORKSPACES_BASE}/namespaces/{namespace}/devworkspaces"
    resp = await _request("POST", ws_url, token, json_body=workspace_payload)
    return resp.json()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Default transport is stdio. For HTTP use: mcp.run(transport="http", port=8000)
    mcp.run()
