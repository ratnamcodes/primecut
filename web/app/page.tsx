export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-24 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">PrimeCut</h1>
      <p className="mt-6 max-w-2xl text-lg leading-8 text-zinc-600 dark:text-zinc-400">
        Drop in a two-hour podcast episode. PrimeCut transcribes it on GPUs,
        reads the transcript with Gemini, watches the frames with a VLM, scores
        every moment against your rubric, then composes two formats from one
        ranking: a 15-minute best-of cut for YouTube and 60-second 9:16
        verticals for Shorts, Reels, and TikTok.
      </p>
    </main>
  );
}
