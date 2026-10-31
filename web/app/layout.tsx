import type { Metadata, Viewport } from "next";
import { Newsreader, Source_Sans_3 } from "next/font/google";

import { siteUrl } from "@/lib/server/config";
import "./globals.css";

// Newsreader: an editorial serif built for reading on screens, used for display type.
// Source Sans 3: a humanist sans with open apertures and tabular figures for the UI.
const display = Newsreader({
  subsets: ["latin"],
  variable: "--font-newsreader",
  display: "swap",
  style: ["normal", "italic"],
});
const sans = Source_Sans_3({
  subsets: ["latin"],
  variable: "--font-source-sans",
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl()),
  title: { default: "Aster Dental Studio", template: "%s · Aster Dental Studio" },
  formatDetection: { telephone: false },
};

export const viewport: Viewport = {
  themeColor: "#f7f4ee",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-IN" className={`${display.variable} ${sans.variable}`}>
      <body className="min-h-dvh antialiased">{children}</body>
    </html>
  );
}
