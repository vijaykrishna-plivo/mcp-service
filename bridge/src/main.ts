/**
 * Multi-tenant MCP bridge over a self-hosted ACI backend.
 *
 * Replicates agent-mcp's URL-based tenant routing AND its HTTP surface so the CX
 * platform (sentinel / livekit / aiassist) can keep its existing wiring:
 *
 *   GET  /                          tool catalog: { servers: { <slug>: { tools } } }
 *   GET  /:appName/credentials-config
 *   POST /:appName/:tenantId        (streamable HTTP, stateless JSON responses)
 *
 * tools/list  → GET  {ACI}/v1/functions/search?app_names=<APP>&format=anthropic
 * tools/call  → POST {ACI}/v1/functions/<NAME>/execute
 *               { function_input, linked_account_owner_id: <owner> }
 *
 * Tokens never reach this process; ACI attaches them server-side.
 *
 * Auth (FAIL-CLOSED — the bridge refuses to start with neither set):
 *   BRIDGE_TENANT_TOKENS  JSON { "<bearer>": "<owner-id>" }. Identity is bound to
 *                         the credential: the URL tenant MUST equal the token's
 *                         owner, and the owner used for ACI comes from the token,
 *                         not the URL. (Recommended — closes cross-tenant access.)
 *   BRIDGE_SECRET_KEY     single shared bearer (legacy; trusts the URL tenant —
 *                         a warning is logged at startup).
 *
 * Env: ACI_API_KEY (required), ACI_URL (default http://localhost:8000),
 *      PORT (default 8101), BRIDGE_APPS_DIR (default ../../apps, for discovery).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
	CallToolRequestSchema,
	ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { toFetchResponse, toReqRes } from "fetch-to-node";
import { Hono } from "hono";

const ACI_URL = process.env.ACI_URL ?? "http://localhost:8000";
const ACI_API_KEY = process.env.ACI_API_KEY ?? "";
const PORT = Number(process.env.PORT ?? 8101);
const APPS_DIR =
	process.env.BRIDGE_APPS_DIR ?? join(import.meta.dir, "..", "..", "apps");

if (!ACI_API_KEY) {
	console.error("ACI_API_KEY is required (printed by scripts/seed.sh)");
	process.exit(1);
}

// --- auth config (fail-closed) -------------------------------------------------
let tenantTokens: Record<string, string> | null = null;
const rawTenantTokens = process.env.BRIDGE_TENANT_TOKENS;
if (rawTenantTokens) {
	try {
		tenantTokens = JSON.parse(rawTenantTokens);
	} catch {
		console.error(
			'BRIDGE_TENANT_TOKENS must be valid JSON: { "<bearer>": "<owner-id>" }',
		);
		process.exit(1);
	}
}
const sharedKey = process.env.BRIDGE_SECRET_KEY || "";
if (!tenantTokens && !sharedKey) {
	console.error(
		"Refusing to start with no auth. Set BRIDGE_TENANT_TOKENS (per-tenant, " +
			"recommended) or BRIDGE_SECRET_KEY (shared). Never expose the bridge unauthenticated.",
	);
	process.exit(1);
}
if (!tenantTokens) {
	console.warn(
		"[security] shared-key mode: the tenant id is trusted from the URL path. " +
			"Set BRIDGE_TENANT_TOKENS to bind tenant identity to the credential.",
	);
}

// A5 — owner-id resolution. The URL tenant (the CX `mcp_id`) IS the ACI
// linked_account_owner_id by default (identity mapping): accounts are linked under
// their mcp_id, so nothing has to be enumerated and it scales with dynamic tenants.
// BRIDGE_OWNER_MAP is an optional JSON { "<mcp_id>": "<aci-owner-id>" } override for
// the rare case the two differ; unmapped tenants fall through to identity.
let ownerMap: Record<string, string> = {};
const rawOwnerMap = process.env.BRIDGE_OWNER_MAP;
if (rawOwnerMap) {
	try {
		ownerMap = JSON.parse(rawOwnerMap);
	} catch {
		console.error(
			'BRIDGE_OWNER_MAP must be valid JSON: { "<mcp_id>": "<aci-owner-id>" }',
		);
		process.exit(1);
	}
}

// agent-mcp server-name → ACI app name. Anything not listed is uppercased as-is
// (google_calendar → GOOGLE_CALENDAR, slack → SLACK, ...).
const APP_ALIASES: Record<string, string> = { zohodesk: "ZOHO_DESK" };
const toAciApp = (slug: string): string =>
	APP_ALIASES[slug.toLowerCase()] ?? slug.toUpperCase();

interface ToolDef {
	name: string;
	description: string;
	input_schema: Record<string, unknown>;
}

// Definitions only change on re-seed; cache briefly to keep tools/list cheap.
const TOOLS_TTL_MS = 60_000;
const toolsCache = new Map<string, { tools: ToolDef[]; at: number }>();

const aciHeaders = {
	"X-API-KEY": ACI_API_KEY,
	"Content-Type": "application/json",
};

async function getTools(app: string): Promise<ToolDef[]> {
	const cached = toolsCache.get(app);
	if (cached && Date.now() - cached.at < TOOLS_TTL_MS) return cached.tools;

	// Use /v1/functions/search (NOT /v1/functions): only the search endpoint
	// honors format=anthropic and returns a clean { name, description,
	// input_schema } where input_schema is the LLM-facing JSON Schema.
	const res = await fetch(
		`${ACI_URL}/v1/functions/search?app_names=${encodeURIComponent(app)}&format=anthropic&limit=1000`,
		{ headers: aciHeaders },
	);
	if (!res.ok) {
		throw new Error(
			`ACI list functions failed (${res.status}): ${await res.text()}`,
		);
	}
	const tools = (await res.json()) as ToolDef[];
	toolsCache.set(app, { tools, at: Date.now() });
	return tools;
}

interface ExecutionResult {
	success: boolean;
	data?: unknown;
	error?: string | null;
}

async function executeTool(
	name: string,
	args: Record<string, unknown>,
	owner: string,
	debugHeaders: Record<string, string>,
): Promise<ExecutionResult> {
	const res = await fetch(
		`${ACI_URL}/v1/functions/${encodeURIComponent(name)}/execute`,
		{
			method: "POST",
			headers: { ...aciHeaders, ...debugHeaders },
			body: JSON.stringify({
				function_input: args,
				linked_account_owner_id: owner,
			}),
		},
	);
	const body = (await res.json().catch(() => null)) as ExecutionResult | null;
	if (!res.ok || !body) {
		return {
			success: false,
			error: `ACI execute failed (${res.status}): ${body ? JSON.stringify(body) : "no body"}`,
		};
	}
	return body;
}

function buildServer(
	app: string,
	owner: string,
	debugHeaders: Record<string, string>,
): Server {
	const server = new Server(
		{ name: `aci-bridge-${app.toLowerCase()}`, version: "0.1.0" },
		{ capabilities: { tools: {} } },
	);

	server.setRequestHandler(ListToolsRequestSchema, async () => {
		const tools = await getTools(app);
		return {
			tools: tools.map((t) => ({
				name: t.name,
				description: t.description,
				inputSchema: t.input_schema,
			})),
		};
	});

	server.setRequestHandler(CallToolRequestSchema, async (request) => {
		const { name, arguments: args } = request.params;
		// #2c — a function may only be invoked through its own app's endpoint;
		// ACI function names are "<APP>__<FUNC>".
		if (!name.startsWith(`${app}__`)) {
			return {
				content: [
					{
						type: "text",
						text: JSON.stringify({
							success: false,
							data: null,
							error: `tool '${name}' is not available on app '${app.toLowerCase()}'`,
						}),
					},
				],
				isError: true,
			};
		}
		const result = await executeTool(name, args ?? {}, owner, debugHeaders);
		// The CX consumers JSON.parse content[0].text and read `.success`, so the
		// text MUST be the { success, data, error } envelope — not the raw data.
		if (!result.success) {
			return {
				content: [
					{
						type: "text",
						text: JSON.stringify({
							success: false,
							data: null,
							error: result.error ?? "execution failed",
						}),
					},
				],
				isError: true,
			};
		}
		return {
			content: [
				{
					type: "text",
					text: JSON.stringify({
						success: true,
						data: result.data ?? null,
						error: null,
					}),
				},
			],
		};
	});

	return server;
}

// --- discovery -----------------------------------------------------------------
// ONE search returns every app's tools (~0.2s). Fanning out one request per app
// saturates ACI's single worker and times out, so the catalog is built from a
// single call and grouped by the "<APP>__" function-name prefix.
const ALL_TOOLS_TTL_MS = 60_000;
let allToolsCache: { tools: ToolDef[]; at: number } | null = null;
async function getAllTools(): Promise<ToolDef[]> {
	if (allToolsCache && Date.now() - allToolsCache.at < ALL_TOOLS_TTL_MS)
		return allToolsCache.tools;
	const res = await fetch(
		`${ACI_URL}/v1/functions/search?format=anthropic&limit=1000`,
		{ headers: aciHeaders },
	);
	if (!res.ok) {
		throw new Error(
			`ACI list all functions failed (${res.status}): ${await res.text()}`,
		);
	}
	const tools = (await res.json()) as ToolDef[];
	allToolsCache = { tools, at: Date.now() };
	return tools;
}

function readAppScheme(slug: string): string | null {
	try {
		const appJson = JSON.parse(
			readFileSync(join(APPS_DIR, slug, "app.json"), "utf8"),
		);
		return Object.keys(appJson.security_schemes ?? {})[0] ?? null;
	} catch {
		return null;
	}
}

// --- HTTP surface --------------------------------------------------------------
type Vars = { Variables: { owner: string } };
const app = new Hono<Vars>();

app.get("/health", (c) => c.json({ ok: true, aci_url: ACI_URL }));

// GET / — full catalog: { servers: { <slug>: { tools: [{name, description}] } } }.
// Mirrors agent-mcp's discovery endpoint that sentinel/aiassist call at startup.
app.get("/", async (c) => {
	let all: ToolDef[];
	try {
		all = await getAllTools();
	} catch (e) {
		return c.json({ servers: {}, error: (e as Error).message }, 502);
	}
	const servers: Record<
		string,
		{ tools: { name: string; description: string }[] }
	> = {};
	for (const t of all) {
		const idx = t.name.indexOf("__");
		if (idx <= 0) continue;
		const slug = t.name.slice(0, idx).toLowerCase();
		(servers[slug] ??= { tools: [] }).tools.push({
			name: t.name,
			description: t.description,
		});
	}
	return c.json({ servers });
});

// GET /:appName/credentials-config — best-effort. No CX consumer calls this at
// runtime today; ACI owns credential collection via its own linking flow.
app.get("/:appName/credentials-config", (c) => {
	const slug = c.req.param("appName");
	const scheme = readAppScheme(slug);
	const credentials_schema =
		scheme === "api_key"
			? {
					type: "object",
					properties: { api_key: { type: "string" } },
					required: ["api_key"],
				}
			: { type: "object", properties: {}, additionalProperties: false };
	return c.json({ server: slug, scheme, credentials_schema });
});

// Auth — enforced on POST only. GET metadata (catalog / credentials-config)
// mirrors agent-mcp's open discovery.
app.use("/:appName/:tenantId", async (c, next) => {
	if (c.req.method !== "POST") return next();
	const authz = c.req.header("Authorization") ?? "";
	const token = authz.startsWith("Bearer ") ? authz.slice(7) : "";
	const urlTenant = c.req.param("tenantId");
	if (tenantTokens) {
		const owner = token ? tenantTokens[token] : undefined;
		if (!owner) return c.json({ error: "unauthorized" }, 401);
		// Bind identity to the credential, not the URL path.
		if (owner !== urlTenant) return c.json({ error: "tenant mismatch" }, 403);
		c.set("owner", owner);
	} else {
		if (token !== sharedKey) return c.json({ error: "unauthorized" }, 401);
		c.set("owner", urlTenant);
	}
	return next();
});

app.post("/:appName/:tenantId", async (c) => {
	const aciApp = toAciApp(c.req.param("appName"));
	// owner comes from the verified credential (or URL in shared-key mode); A5 maps
	// the mcp_id to the ACI owner-id, defaulting to identity.
	const owner = ownerMap[c.get("owner")] ?? c.get("owner");

	const debugHeaders: Record<string, string> = {};
	const flowRunUuid = c.req.header("x-debug-flow-run-uuid");
	if (flowRunUuid) debugHeaders["x-debug-flow-run-uuid"] = flowRunUuid;

	// The transport gets the parsed body explicitly, so wrap a BODY-LESS copy of
	// the request: fetch-to-node lazily reads the wrapped stream after the
	// response, and finding it locked/consumed by c.req.json() kills bun.
	const requestBody = await c.req.json();
	const { req, res } = toReqRes(
		new Request(c.req.raw.url, {
			method: "POST",
			headers: c.req.raw.headers,
		}),
	);

	try {
		const server = buildServer(aciApp, owner, debugHeaders);
		const transport = new StreamableHTTPServerTransport({
			sessionIdGenerator: undefined,
			enableJsonResponse: true,
		});
		await server.connect(transport);

		await transport.handleRequest(req, res, requestBody);
		res.on("close", () => {
			transport.close();
			server.close();
		});
		return toFetchResponse(res);
	} catch (error) {
		console.error(
			`[${aciApp}/${owner}] error handling MCP request:`,
			(error as Error).message,
		);
		if (!res.headersSent) {
			return c.json(
				{
					jsonrpc: "2.0",
					error: { code: -32603, message: "Internal server error" },
					id: null,
				},
				500,
			);
		}
	}
});

// idleTimeout (s): cold-cache GET / fans out to every app's tools/list against
// ACI; the default 10s can cut that off. Discovery is cached after the first call.
export default { port: PORT, fetch: app.fetch, idleTimeout: 120 };
console.log(`aci-mcp-bridge listening on :${PORT} → ${ACI_URL}`);
console.log(
	"MCP endpoint: POST /<app>/<tenant-id>  (e.g. /google_calendar/cx-tenant)",
);
console.log("Catalog: GET /   ·   health: GET /health");
