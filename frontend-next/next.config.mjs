/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The Django backend is the only origin the browser needs to reach for data.
  async rewrites() {
    const backend = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
