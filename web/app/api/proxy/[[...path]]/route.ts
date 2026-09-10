import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Python backend base URL (server-side env). */
const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

async function proxy(req: NextRequest, path: string[]) {
  const suffix = path.length ? `/${path.join("/")}` : "";
  const url = `${BACKEND}/api${suffix}${req.nextUrl.search}`;
  const headers: Record<string, string> = { accept: "application/json" };
  const body =
    req.method === "POST" || req.method === "PUT" || req.method === "PATCH"
      ? await req.text()
      : undefined;
  if (body) {
    headers["content-type"] = req.headers.get("content-type") ?? "application/json";
  }
  try {
    const upstream = await fetch(url, { method: req.method, headers, body });
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    return NextResponse.json(
      {
        error: "backend unreachable",
        detail: `Could not reach the trading backend at ${BACKEND}. Start it with: uvicorn server.main:app --host 0.0.0.0 --port 8000`,
      },
      { status: 502 },
    );
  }
}

function extractPath(params: { path?: string[] }): string[] {
  return Array.isArray(params.path) ? params.path : [];
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const params = await ctx.params;
  return proxy(req, extractPath(params));
}

export async function POST(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const params = await ctx.params;
  return proxy(req, extractPath(params));
}