import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PrimeCut",
  description:
    "Drop in a two-hour podcast episode. PrimeCut transcribes it on GPUs, reads the transcript with Gemini, watches the frames with a VLM, scores every moment against your rubric, then composes two formats from one ranking: a 15-minute best-of cut for YouTube and 60-second 9:16 verticals for Shorts, Reels, and TikTok.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">{children}</body>
    </html>
  );
}
