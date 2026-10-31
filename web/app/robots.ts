import type { MetadataRoute } from "next";

import { siteUrl } from "@/lib/server/config";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/staff", "/api/"] }],
    sitemap: `${siteUrl()}/sitemap.xml`,
  };
}
