import type { NextConfig } from "next";

// When running inside Docker, use service names; otherwise use localhost
const analyticsApiHost = process.env.ANALYTICS_API_HOST || '127.0.0.1';
const ingestionApiHost = process.env.INGESTION_API_HOST || '127.0.0.1';

const nextConfig: NextConfig = {
  rewrites: async () => {
    return {
      beforeFiles: [],
      afterFiles: [
        {
          source: '/ingest/:path*',
          destination: `http://${ingestionApiHost}:8000/:path*`,
        },
      ],
      fallback: [
        {
          // Proxy /api/* to FastAPI backend EXCEPT /api/auth/* which is NextAuth
          // Using a negative lookahead-equivalent: match /api/ followed by anything
          // that does NOT start with "auth"
          source: '/api/((?!auth/).*)',
          destination: `http://${analyticsApiHost}:8001/:path*`,
        },
      ],
    };
  },
};

export default nextConfig;
