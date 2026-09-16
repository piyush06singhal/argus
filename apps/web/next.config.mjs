/**
 * Forward-proxy target for the /api route. In local dev this is the host API
 * running on localhost; in Docker Compose it is the 'api' service on the shared
 * network. The browser never sees this — Next rewrites server-side.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const target = process.env.API_PROXY_TARGET ?? 'http://localhost:8000';
    return [
      {
        source: '/api/:path*',
        // Keep the /api prefix: /api/v1/projects -> {target}/api/v1/projects
        destination: `${target}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;