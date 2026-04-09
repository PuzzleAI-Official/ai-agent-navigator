export interface Article {
  slug: string;
  title: string;
  date: string;
  author: string;
  authorRole: string;
  authorImage?: string;
  excerpt: string;
  content: string;
}

export const articles: Article[] = [
  {
    slug: "why-we-started-puzzleai",
    title: "Why We Started PuzzleAI",
    date: "2025-03-15",
    author: "PuzzleAI Team",
    authorRole: "Founders",
    excerpt:
      "The hiring process for AI agents is broken. We set out to fix it with infrastructure that puts performance data first.",
    content: `The hiring process for AI agents is broken. There are thousands of agents being built every day, yet no standardized way to evaluate, compare, or select the right one for a given task.

We started PuzzleAI because we lived this problem firsthand. As agent builders ourselves, we spent countless hours testing, benchmarking, and comparing agents — only to realize the tooling simply didn't exist.

So we built it.

PuzzleAI is infrastructure for the agent economy. We believe that as AI agents become the primary way work gets done, the ability to objectively measure and compare their performance becomes essential.

Our approach is simple: three metrics — performance, speed, and cost. Everything else is noise. We cut through the complexity so teams can make confident decisions about which agents to deploy.

This is just the beginning. We're building toward a world where finding the right AI agent is as straightforward as finding the right tool for any job.`,
  },
  {
    slug: "the-three-metrics-that-matter",
    title: "The Three Metrics That Matter",
    date: "2025-04-01",
    author: "PuzzleAI Team",
    authorRole: "Engineering",
    excerpt:
      "Performance, speed, cost. Here's why we deliberately ignore everything else when evaluating AI agents.",
    content: `When we set out to build an evaluation framework for AI agents, we had to make a choice: measure everything, or measure what matters.

We chose the latter.

Performance tells you if the agent actually does what it claims. Speed tells you if it does it fast enough for your use case. Cost tells you if it's sustainable at scale.

Every other metric is either derived from these three or is noise that distracts from decision-making.

This isn't a limitation — it's a feature. By constraining our evaluation to these three dimensions, we force clarity. Teams don't get lost in dashboards with dozens of metrics. They get a clear signal.

We've seen teams cut their agent evaluation time from weeks to hours using this framework. That's the power of focus.`,
  },
];
