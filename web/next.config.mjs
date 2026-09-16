/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a fully static export (web/out/) so the FastAPI backend can serve the
  // UI and the API from a single service/port (Railway, Docker, localhost).
  output: "export",
  poweredByHeader: false,
  reactStrictMode: true,
  images: { unoptimized: true },
  // `rewrites()` only run in `next dev`/`next start` (a server), not for a
  // static export build - hence the dev-only guard below. In production the
  // FastAPI backend serves the exported UI at "/" alongside /api and /ws from
  // the same origin, so no rewrite/proxy is needed at all.
  async rewrites() {
    if (process.env.NODE_ENV === "production") return [];
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
      {
        source: "/ws",
        destination: "http://localhost:8000/ws",
      },
    ];
  },
};

export default nextConfig;
