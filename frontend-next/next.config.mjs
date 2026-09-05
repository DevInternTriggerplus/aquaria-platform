/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The Django backend is the only origin the browser needs to reach for data.
  async rewrites() {
    const backend = process.env.BACKEND_ORIGIN ?? (process.env.VERCEL ? "" : "http://127.0.0.1:8000");
    if (!backend) return [];
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
