# WONRAE Codex agent backend

The FastAPI API, map data, authentication and travel tools remain the application
backend. With `AGENT_BACKEND=codex`, the official `openai-codex` Python SDK and
its pinned Codex App Server run the reasoning and tool loop for both travel chat
and city batches. `groq` explicitly selects the legacy runner for rollback.

## Runtime and memory

Each invocation starts an isolated, ephemeral Codex thread with a private temporary
`CODEX_HOME`. Shell execution, plugins, apps, extra agents and built-in web search
are disabled. The model calls the application's city-scoped travel MCP server.
Credentials never enter the user prompt or a committed configuration file.

SQL records are durable memory: user/city chat history, `TravelChatWork`, proposals,
knowledge, tasks and batch run steps. A fresh container rebuilds its context from
those records. Previously generated assistant prose is conversation context, not
proof of a place's identity or location. Temporary harness sessions are not a
second long-term store for discovery-only provider data.

The agent chooses what to investigate and how to react to tool errors. Business
validation remains on the server: city boundaries, observed coordinates, exact
branch evidence, approval proposals and recorded outcomes. Direct creation and
merging of places are not exposed by this integration.

## Configuration

```dotenv
AGENT_BACKEND=codex
CODEX_API_KEY=<injected by ECS from Secrets Manager>
CODEX_MODEL=gpt-6.1-sol
CODEX_TIMEOUT_SECONDS=900
CODEX_BATCH_TIMEOUT_SECONDS=7200
```

Local development can use `OPENAI_API_KEY` instead of `CODEX_API_KEY`. The runtime
uses the latter first. Use `dev/set-openai-secret.ps1` to register a server key
without placing it in shell arguments or source files. The ECS tasks need at
least 1024 MiB of memory in Codex mode.

The browser submits a chat job and polls its persisted result, avoiding an idle
HTTP request across a long agent turn. Completed tool work remains in the ledger
even if a model invocation fails or the browser disconnects. Paid scheduled
research is controlled separately by Terraform `agent_schedule_enabled`.

## Verify and deploy

From `backend`, run `python -m unittest discover -s tests`. From `frontend`, run
`npm run build`. Then run `terraform fmt -check` and `terraform validate` in
`infra`. The deployment workflow runs backend tests before building an image;
manual `deploy=false` builds an image while automatic deployment is disabled.

Build and push the image before applying a plan with `deployment_mode=active`
and `agent_backend=codex`. Check login, map contents, a chat tool call, a follow-up
and a small manual city batch before enabling paid schedules.

Official integration references: [Python SDK](https://learn.chatgpt.com/docs/codex-sdk),
[App Server](https://learn.chatgpt.com/docs/app-server),
[MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
