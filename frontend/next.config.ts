import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  assetPrefix: "/assets",
  images: { unoptimized: true },
  poweredByHeader: false,
};

export default nextConfig;
