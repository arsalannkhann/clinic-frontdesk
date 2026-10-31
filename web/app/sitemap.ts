import type { MetadataRoute } from "next";

import { siteUrl } from "@/lib/server/config";

export default function sitemap(): MetadataRoute.Sitemap {
  const base = siteUrl();
  return [
    { url: `${base}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${base}/book`, changeFrequency: "daily", priority: 0.9 },
    { url: `${base}/booking`, changeFrequency: "monthly", priority: 0.4 },
    { url: `${base}/callback`, changeFrequency: "monthly", priority: 0.5 },
  ];
}
