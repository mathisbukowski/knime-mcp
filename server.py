#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "websockets>=12"]
# ///
"""MCP server for a local KNIME Analytics Platform.

Two groups of tools:

* File tools read saved workflows from the workspace. KNIME can be closed.
* Live tools edit and execute workflows inside a running KNIME, through the
  gateway its own UI uses. KNIME must be started with
  -Dchromium.remote_debugging_port=<KNIME_DEBUG_PORT>.

The gateway is internal to KNIME and can change between releases.
"""
import asyncio
import itertools
import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import websockets
from mcp.server.fastmcp import FastMCP

WORKSPACE = Path(os.environ.get("KNIME_WORKSPACE", "~/knime-workspace")).expanduser().resolve()
DEBUG_PORT = int(os.environ.get("KNIME_DEBUG_PORT", "9222"))
NS = "{http://www.knime.org/2008/09/XMLConfig}"
RPC_ACTION = "org.knime.ui.java.jsonrpc"
CURRENT = "current-state"
RPC_TIMEOUT_SECONDS = 30
STORE_SYNC_SECONDS = 0.4
APP = "document.querySelector('#app').__vue_app__.config.globalProperties"
ACTIVE = f"{APP}.$pinia.state.value.workflow.activeWorkflow"
BUSY_STATES = {"QUEUED", "EXECUTING"}

mcp = FastMCP("knime")


def resolve(workflow: str) -> Path:
    """Resolve a workspace-relative workflow path and refuse anything outside."""
    path = (WORKSPACE / workflow).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError(f"Path outside workspace: {workflow}")
    if not (path / "workflow.knime").is_file():
        raise ValueError(f"No workflow.knime in: {workflow}")
    return path


def to_python(element: ET.Element):
    """Convert a KNIME XMLConfig element to nested dicts."""
    result = {}
    for child in element:
        key = child.get("key")
        if child.tag == f"{NS}entry":
            result[key] = None if child.get("isnull") == "true" else child.get("value")
        elif child.tag == f"{NS}config":
            result[key] = to_python(child)
    return result


def load(path: Path) -> dict:
    return to_python(ET.parse(path).getroot())


def node_summary(workflow_dir: Path, node: dict) -> dict:
    settings_file = node.get("node_settings_file") or ""
    folder = settings_file.rsplit("/", 1)[0]
    summary = {"id": node.get("id"), "folder": folder, "type": node.get("node_type")}
    settings_path = workflow_dir / settings_file
    if settings_path.is_file():
        settings = load(settings_path)
        summary["name"] = settings.get("node-name") or settings.get("name")
        summary["factory"] = settings.get("factory")
        summary["state"] = settings.get("state")
    summary["is_nested"] = (workflow_dir / folder / "workflow.knime").is_file()
    return summary


def read_nodes(workflow_dir: Path) -> tuple[dict, list[dict]]:
    data = load(workflow_dir / "workflow.knime")
    nodes = [node_summary(workflow_dir, n) for n in data.get("nodes", {}).values()]
    return data, nodes


@mcp.tool()
def list_workflows() -> list[str]:
    """List workflows in the KNIME workspace, as workspace-relative paths."""
    found = []
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "workflow.knime" in files:
            found.append(str(Path(root).relative_to(WORKSPACE)))
            dirs.clear()
    return sorted(found)


@mcp.tool()
def get_workflow(workflow: str) -> dict:
    """Return nodes, connections and state of a saved workflow.

    Reflects the last save, not unsaved changes in the KNIME GUI. For a
    component or metanode, pass "<workflow>/<node folder>".
    """
    path = resolve(workflow)
    data, nodes = read_nodes(path)
    connections = [
        {
            "from": c.get("sourceID"),
            "from_port": c.get("sourcePort"),
            "to": c.get("destID"),
            "to_port": c.get("destPort"),
        }
        for c in data.get("connections", {}).values()
    ]
    return {"state": data.get("state"), "nodes": nodes, "connections": connections}


@mcp.tool()
def get_node_settings(workflow: str, node_id: str) -> dict:
    """Return the full saved configuration of one node, by its numeric id."""
    path = resolve(workflow)
    data = load(path / "workflow.knime")
    for node in data.get("nodes", {}).values():
        if node.get("id") == str(node_id):
            return load(path / node["node_settings_file"])
    raise ValueError(f"Node {node_id} not found in {workflow}")


class Gateway:
    """One DevTools session on the KNIME UI page."""

    def __init__(self):
        self.ids = itertools.count(1)

    async def __aenter__(self):
        url = f"http://127.0.0.1:{DEBUG_PORT}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                targets = json.load(response)
        except (urllib.error.URLError, OSError) as error:
            raise RuntimeError(
                f"KNIME is not reachable on port {DEBUG_PORT}. Start it with "
                f"-Dchromium.remote_debugging_port={DEBUG_PORT}."
            ) from error
        pages = [t for t in targets if "org.knime.ui.java/editor" in t.get("url", "")]
        if not pages:
            raise RuntimeError("KNIME UI page not found. Is KNIME still starting?")
        self.socket = await websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None)
        return self

    async def __aexit__(self, *_):
        await self.socket.close()

    async def evaluate(self, expression: str):
        message_id = next(self.ids)
        await self.socket.send(json.dumps({
            "id": message_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
        }))
        async with asyncio.timeout(RPC_TIMEOUT_SECONDS):
            while True:
                message = json.loads(await self.socket.recv())
                if message.get("id") == message_id:
                    result = message["result"]
                    if "exceptionDetails" in result:
                        raise RuntimeError(str(result["exceptionDetails"])[:500])
                    return result["result"].get("value")

    async def call(self, method: str, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        raw = await self.evaluate(
            f"window.EquoCommService.send({json.dumps(RPC_ACTION)}, {json.dumps(body)})"
        )
        response = json.loads(raw)
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error'].get('message')}")
        return response.get("result")

    async def project(self, project_id: str | None) -> str:
        """Return the given project, or the only open one, and show it in the UI."""
        project = await self.resolve_project(project_id)
        await self.show(project)
        return project

    async def resolve_project(self, project_id: str | None) -> str:
        if project_id:
            return project_id
        state = await self.call("ApplicationService.getState")
        projects = state.get("openProjects", [])
        active = [p for p in projects if p.get("activeWorkflowId")]
        if len(active) == 1:
            return active[0]["projectId"]
        if len(projects) == 1:
            return projects[0]["projectId"]
        raise RuntimeError("Pass project_id. Open projects: " + json.dumps(
            [{"project_id": p["projectId"], "name": p["name"]} for p in projects]
        ))

    async def show(self, project: str):
        """Make the project the active tab, so the UI store tracks it."""
        target = json.dumps(project)
        await self.evaluate(
            f"{ACTIVE}?.projectId === {target} ? null : {APP}.$router.push("
            f"{{name: 'WorkflowPage', params: {{projectId: {target}, workflowId: 'root'}}}})"
            ".then(() => null)"
        )
        for _ in range(40):
            if await self.evaluate(f"{ACTIVE}?.projectId") == project:
                return
            await asyncio.sleep(0.25)
        raise RuntimeError(f"Project {project} did not become active")

    async def workflow(self, project_id: str, workflow_id: str) -> dict:
        """Read a workflow.

        The root comes from the UI store. Repeated getWorkflow calls evict the
        snapshot that the UI follows, which makes workflow commands hang.
        """
        if workflow_id == "root":
            return json.loads(await self.evaluate(f"JSON.stringify({ACTIVE})"))
        result = await self.call(
            "WorkflowService.getWorkflow", projectId=project_id, workflowId=workflow_id,
            versionId=CURRENT, includeInteractionInfo=True,
        )
        return result["workflow"]

    async def command(self, project_id: str, workflow_id: str, **workflow_command):
        result = await self.call(
            "WorkflowService.executeWorkflowCommand", projectId=project_id,
            workflowId=workflow_id, workflowCommand=workflow_command,
        )
        await asyncio.sleep(STORE_SYNC_SECONDS)
        return result


def merge(base: dict, patch: dict) -> dict:
    """Recursively apply patch to base."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def node_states(workflow: dict) -> dict:
    states = {}
    for node_id, node in workflow.get("nodes", {}).items():
        state = node.get("state") or {}
        states[node_id] = {
            key: value
            for key, value in {
                "state": state.get("executionState"),
                "error": state.get("error"),
                "warning": state.get("warning"),
            }.items()
            if value
        }
    return states


async def load_dialog(gateway: Gateway, ids: dict, node_id: str) -> dict:
    dialog = await gateway.call("NodeService.getNodeDialog", **ids, nodeId=node_id)
    if dialog.get("deactivationRequired"):
        await gateway.call(
            "NodeService.deactivateNodeDataServices", **ids, nodeId=node_id, extensionType="dialog"
        )
    return json.loads(dialog["initialData"])["result"]


async def require_web_dialog(gateway: Gateway, project: str, workflow_id: str, node_id: str):
    node = (await gateway.workflow(project, workflow_id))["nodes"].get(node_id)
    if node is None:
        raise ValueError(f"Node {node_id} not found")
    if node.get("dialogType") != "web":
        raise ValueError(
            f"Node {node_id} has dialog type {node.get('dialogType')!r}. "
            "Only web dialogs can be read or changed. Configure this node in KNIME."
        )


@mcp.tool()
async def live_list_projects() -> list[dict]:
    """List workflows open in the running KNIME."""
    async with Gateway() as gateway:
        state = await gateway.call("ApplicationService.getState")
    return [
        {"project_id": p["projectId"], "name": p["name"], "active": bool(p.get("activeWorkflowId"))}
        for p in state.get("openProjects", [])
    ]


@mcp.tool()
async def live_create_workflow(name: str) -> dict:
    """Create a workflow at the root of the local space and open it."""
    async with Gateway() as gateway:
        item = await gateway.call(
            "SpaceService.createWorkflow", spaceProviderId="local", spaceId="local",
            itemId="root", itemName=name,
        )
        return await open_item(gateway, item["id"])


async def open_item(gateway: Gateway, item_id: str) -> dict:
    await gateway.evaluate(
        f"Promise.resolve(window.openProject('local', {json.dumps(item_id)}, 'local'))"
        ".then(() => null)"
    )
    for _ in range(60):
        state = await gateway.call("ApplicationService.getState")
        for project in state.get("openProjects", []):
            if project.get("origin", {}).get("itemId") == item_id:
                return {"project_id": project["projectId"], "name": project["name"]}
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Item {item_id} did not open")


@mcp.tool()
async def live_open_workflow(path: str) -> dict:
    """Open a workflow of the local space, by workspace-relative path."""
    async with Gateway() as gateway:
        item_id = "root"
        for part in Path(path).parts:
            group = await gateway.call(
                "SpaceService.listWorkflowGroup", spaceProviderId="local", spaceId="local",
                itemId=item_id,
            )
            match = [i for i in group["items"] if i["name"] == part]
            if not match:
                raise ValueError(f"{part!r} not found in local space")
            item_id = match[0]["id"]
        return await open_item(gateway, item_id)


@mcp.tool()
async def live_get_workflow(project_id: str | None = None, workflow_id: str = "root") -> dict:
    """Return nodes, connections and states, including unsaved changes.

    Node ids look like root:12. For a metanode or component, pass its node id
    as workflow_id.
    """
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        workflow = await gateway.workflow(project, workflow_id)
    templates = workflow.get("nodeTemplates", {})
    nodes = []
    for node_id, node in workflow.get("nodes", {}).items():
        template = templates.get(node.get("templateId"), {})
        nodes.append({
            "id": node_id,
            "name": template.get("name") or node.get("name"),
            "kind": node.get("kind"),
            "dialog": node.get("dialogType"),
            "state": (node.get("state") or {}).get("executionState"),
            "ports": [
                {"index": p["index"], "name": p.get("name"), "info": p.get("info")}
                for p in node.get("outPorts", [])
            ],
        })
    connections = [
        {
            "id": connection_id,
            "from": c["sourceNode"],
            "from_port": c["sourcePort"],
            "to": c["destNode"],
            "to_port": c["destPort"],
        }
        for connection_id, c in workflow.get("connections", {}).items()
    ]
    return {"project_id": project, "nodes": nodes, "connections": connections}


@mcp.tool()
async def live_search_nodes(query: str, limit: int = 10) -> list[dict]:
    """Search installed nodes. Use the returned factory with live_add_node."""
    async with Gateway() as gateway:
        result = await gateway.call(
            "NodeRepositoryService.searchNodes", q=query, tags=None, allTagsMatch=True,
            offset=0, limit=limit, fullTemplateInfo=False, portTypeId=None, nodeRelation=None,
        )
    return [{"name": n["name"], "factory": n["id"], "type": n.get("type")} for n in result["nodes"]]


@mcp.tool()
async def live_add_node(
    factory: str, x: int, y: int, project_id: str | None = None, workflow_id: str = "root"
) -> dict:
    """Add a node at canvas position (x, y) and return its node id."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        result = await gateway.command(
            project, workflow_id, kind="add_node", position={"x": x, "y": y},
            nodeFactory={"className": factory},
        )
    return {"node_id": result["newNodeId"]}


@mcp.tool()
async def live_connect_nodes(
    source_node: str, source_port: int, target_node: str, target_port: int,
    project_id: str | None = None, workflow_id: str = "root",
) -> str:
    """Connect two nodes. Port 0 is the flow variable port, data starts at 1."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await gateway.command(
            project, workflow_id, kind="connect", sourceNodeId=source_node,
            sourcePortIdx=source_port, destinationNodeId=target_node,
            destinationPortIdx=target_port,
        )
    return "connected"


@mcp.tool()
async def live_delete(
    node_ids: list[str] | None = None, connection_ids: list[str] | None = None,
    project_id: str | None = None, workflow_id: str = "root",
) -> str:
    """Delete nodes or connections. KNIME can undo this until the project closes."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await gateway.command(
            project, workflow_id, kind="delete", nodeIds=node_ids or [],
            connectionIds=connection_ids or [], annotationIds=[],
        )
    return "deleted"


@mcp.tool()
async def live_get_node_config(
    node_id: str, project_id: str | None = None, workflow_id: str = "root"
) -> dict:
    """Return settings and their JSON schema. Only for nodes with a web dialog."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await require_web_dialog(gateway, project, workflow_id, node_id)
        ids = {"projectId": project, "workflowId": workflow_id, "versionId": CURRENT}
        dialog = await load_dialog(gateway, ids, node_id)
    return {"settings": dialog["data"], "schema": dialog["schema"]}


@mcp.tool()
async def live_set_node_config(
    node_id: str, settings: dict, project_id: str | None = None, workflow_id: str = "root"
) -> dict:
    """Merge settings into the node configuration and apply it.

    Pass only changed keys, nested as in live_get_node_config. Applying can
    reset the node and everything downstream.
    """
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await require_web_dialog(gateway, project, workflow_id, node_id)
        ids = {"projectId": project, "workflowId": workflow_id, "versionId": CURRENT}
        dialog = await load_dialog(gateway, ids, node_id)
        request = json.dumps({
            "data": merge(dialog["data"], settings),
            "flowVariableSettings": dialog.get("flowVariableSettings", {}),
        })
        raw = await gateway.call(
            "NodeService.callNodeDataService", **ids, nodeId=node_id, extensionType="dialog",
            serviceType="apply_data", dataServiceRequest=request,
        )
    return json.loads(raw)


async def change_states(project_id, workflow_id, node_ids, action, timeout_seconds) -> dict:
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await gateway.call(
            "NodeService.changeNodeStates", projectId=project, workflowId=workflow_id,
            nodeIds=node_ids or [], action=action,
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            await asyncio.sleep(0.5 if workflow_id == "root" else 3)
            states = node_states(await gateway.workflow(project, workflow_id))
            busy = [i for i, s in states.items() if s.get("state") in BUSY_STATES]
            if not busy:
                return {"finished": True, "nodes": states}
            if time.monotonic() > deadline:
                return {"finished": False, "still_running": busy, "nodes": states}


@mcp.tool()
async def live_execute(
    node_ids: list[str] | None = None, timeout_seconds: int = 120,
    project_id: str | None = None, workflow_id: str = "root",
) -> dict:
    """Execute nodes, or all nodes if none are given, and wait for the result."""
    return await change_states(project_id, workflow_id, node_ids, "execute", timeout_seconds)


@mcp.tool()
async def live_reset(
    node_ids: list[str] | None = None, project_id: str | None = None, workflow_id: str = "root"
) -> dict:
    """Reset nodes, or all nodes if none are given."""
    return await change_states(project_id, workflow_id, node_ids, "reset", 30)


@mcp.tool()
async def live_read_table(
    node_id: str, port: int = 1, offset: int = 0, limit: int = 50,
    project_id: str | None = None, workflow_id: str = "root",
) -> dict:
    """Read rows from the table at an executed node's output port."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        ids = {"projectId": project, "workflowId": workflow_id, "versionId": CURRENT}
        target = {**ids, "nodeId": node_id, "portIdx": port, "viewIdx": 1}
        view = await gateway.call("PortService.getPortView", **target)
        initial = json.loads(view["initialData"])["result"]["table"]
        columns = initial["displayedColumns"]
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getTable",
            "params": [columns, offset, limit, [], True, True, False, False],
        })
        raw = await gateway.call(
            "PortService.callPortDataService", **target, serviceType="data",
            dataServiceRequest=request,
        )
        if view.get("deactivationRequired"):
            await gateway.call("PortService.deactivatePortDataServices", **target)
    table = json.loads(raw)["result"]
    # Each row starts with the row index and the row key.
    return {
        "columns": columns,
        "row_count": initial["rowCount"],
        "rows": [row[2:] for row in table["rows"]],
    }


@mcp.tool()
async def live_save(project_id: str | None = None) -> str:
    """Save a project to disk, overwriting the saved version."""
    async with Gateway() as gateway:
        project = await gateway.project(project_id)
        await gateway.evaluate(
            f"Promise.resolve(window.saveProject({json.dumps(project)}, false)).then(() => null)"
        )
        for _ in range(60):
            if not (await gateway.workflow(project, "root")).get("dirty"):
                return "saved"
            await asyncio.sleep(0.5)
    raise RuntimeError("Save did not finish within 30 seconds")


if __name__ == "__main__":
    mcp.run()
