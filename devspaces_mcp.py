from fastmcp import FastMCP
import httpx
import os
import subprocess
import asyncio
import json
from typing import Optional, List, Dict

# FastMCP server instance
mcp = FastMCP("DevSpaces MCP Server")

# Base API URL for DevWorkspace resources
BASE_URL = "https://api.ocp.v7hjl.sandbox2288.opentlc.com:6443/apis/workspace.devfile.io/v1alpha2"


def _get_token(token: Optional[str] = None) -> str:
    """Return the OpenShift token, using the provided value, env var, or oc CLI."""
    if token:
        return token
    env_token = os.environ.get("OPENSHIFT_TOKEN")
    if env_token:
        return env_token
    # Fallback to oc whoami -t (may raise if oc not installed)
    return subprocess.check_output(["oc", "whoami", "-t"], text=True).strip()


def _compact_workspace(data: Dict) -> Dict:
    """Extract the compact fields required for the MCP response."""
    metadata = data.get("metadata", {})
    status = data.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "phase": status.get("phase"),
        "mainUrl": status.get("mainUrl"),
        "created": metadata.get("creationTimestamp"),
    }


async def _request(
    method: str,
    url: str,
    token: str,
    json_body: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    patch: bool = False,
) -> httpx.Response:
    """Send an HTTP request to the OpenShift API with proper defaults and error handling."""
    default_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        default_headers.update(headers)
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        try:
            if method == "GET":
                resp = await client.get(url, headers=default_headers)
            elif method == "POST":
                resp = await client.post(url, json=json_body, headers=default_headers)
            elif method == "DELETE":
                resp = await client.delete(url, headers=default_headers)
            elif method == "PATCH":
                # For patch we need to set content-type if not supplied
                if "Content-Type" not in default_headers:
                    default_headers["Content-Type"] = "application/merge-patch+json"
                resp = await client.patch(url, content=json.dumps(json_body), headers=default_headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            # Wrap error in a response-like object with .text for simplicity
            class ErrResp:
                def __init__(self, err_msg: str):
                    self.status_code = 0
                    self.text = err_msg
                def json(self):
                    return {"error": err_msg}
            raise exc


# ------------------- Tool Definitions -------------------

@mcp.tool
async def list_workspaces(namespace: str, token: Optional[str] = None) -> List[Dict]:
    """List DevWorkspaces in the given namespace.

    Returns a list of compact workspace dictionaries.
    """
    token_val = _get_token(token)
    url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces"
    resp = await _request("GET", url, token_val)
    data = resp.json()
    items = data.get("items", [])
    return [_compact_workspace(ws) for ws in items]


@mcp.tool
async def get_workspace(namespace: str, name: str, token: Optional[str] = None) -> Dict:
    """Retrieve a single DevWorkspace.

    Returns a compact workspace dictionary.
    """
    token_val = _get_token(token)
    url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces/{name}"
    resp = await _request("GET", url, token_val)
    return _compact_workspace(resp.json())


@mcp.tool
async def delete_workspace(namespace: str, name: str, token: Optional[str] = None) -> Dict:
    """Delete a DevWorkspace.

    Returns a dict with a success flag or error information.
    """
    token_val = _get_token(token)
    url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces/{name}"
    resp = await _request("DELETE", url, token_val)
    if resp.status_code == 200:
        return {"deleted": True, "name": name, "namespace": namespace}
    return {"deleted": False, "status_code": resp.status_code, "detail": resp.text}


@mcp.tool
async def start_workspace(namespace: str, name: str, token: Optional[str] = None) -> Dict:
    """Start (or resume) a DevWorkspace via a PATCH request."""
    token_val = _get_token(token)
    url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces/{name}"
    payload = {"spec": {"started": True}}
    resp = await _request("PATCH", url, token_val, json_body=payload)
    return _compact_workspace(resp.json())


@mcp.tool
async def stop_workspace(namespace: str, name: str, token: Optional[str] = None) -> Dict:
    """Stop (pause) a DevWorkspace via a PATCH request."""
    token_val = _get_token(token)
    url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces/{name}"
    payload = {"spec": {"started": False}}
    resp = await _request("PATCH", url, token_val, json_body=payload)
    return _compact_workspace(resp.json())


@mcp.tool
async def create_workspace(
    namespace: str,
    ws_name: str,
    git_repo_url: str,
    token: Optional[str] = None,
) -> Dict:
    """Create a DevWorkspace and its associated IDE template.

    The function first creates a DevWorkspaceTemplate, waits briefly, then creates the DevWorkspace.
    Returns the compact representation of the created workspace.
    """
    token_val = _get_token(token)
    # 1️⃣ Create DevWorkspaceTemplate
    template_payload = {
        "apiVersion": "workspace.devfile.io/v1alpha2",
        "kind": "DevWorkspaceTemplate",
        "metadata": {"name": f"{ws_name}-ide", "namespace": namespace},
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
    tmpl_url = f"{BASE_URL}/namespaces/{namespace}/devworkspacetemplates"
    await _request("POST", tmpl_url, token_val, json_body=template_payload)

    # Small pause to allow the API to register the template
    await asyncio.sleep(2)

    # 2️⃣ Create DevWorkspace referencing the template
    workspace_payload = {
        "apiVersion": "workspace.devfile.io/v1alpha2",
        "kind": "DevWorkspace",
        "metadata": {"name": ws_name, "namespace": namespace},
        "spec": {
            "routingClass": "che",
            "started": True,
            "contributions": [
                {"name": "editor", "kubernetes": {"name": f"{ws_name}-ide"}}
            ],
            "template": {
                "projects": [
                    {"name": "project", "git": {"remotes": {"origin": git_repo_url}}}
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
    ws_url = f"{BASE_URL}/namespaces/{namespace}/devworkspaces"
    resp = await _request("POST", ws_url, token_val, json_body=workspace_payload)
    return _compact_workspace(resp.json())


if __name__ == "__main__":
    mcp.run()
