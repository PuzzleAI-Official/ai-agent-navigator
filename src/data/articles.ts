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
    date: "2026-04-01",
    author: "Lianming Hu",
    authorRole: "Founders",
    excerpt:
      "The inspiration for building A2A infrastructure is not coming from nowhere. It is naturally rooted in the work: the team has been building agentic services and found that the current infrastructure workflow is fragmented.",
    content: `The inspiration for building A2A infrastructure is not coming from nowhere. It is naturally rooted in the work: the team has been building agentic services and found that the current infrastructure workflow is fragmented.
    
We started PuzzleAI because we lived this problem firsthand. As agent builders ourselves, we spent countless hours testing, benchmarking, and comparing agents — only to realize the tooling simply didn't exist.

So we built it.

PuzzleAI is infrastructure for the agent economy. We believe that as AI agents become the primary way work gets done, the ability to objectively measure and compare their performance becomes essential.

Our approach is simple: three metrics — performance, speed, and cost. Everything else is noise. We cut through the complexity so teams can make confident decisions about which agents to deploy.

This is just the beginning. We're building toward a world where finding the right AI agent is as straightforward as finding the right tool for any job.`,
  },
  {
    slug: "the-three-metrics-that-matter",
    title: "The Three Metrics That Matter",
    date: "2026-04-09",
    author: "Jeffrey Wu",
    authorRole: "Engineering",
    excerpt:
      "Before Stripe, accepting payments online was technically painful, fragmented, and trust-broken. Merchants existed. Buyers existed. The infrastructure layer that made the transaction trustworthy at scale didn't. Stripe built it — and became a $95B company without owning a single product being sold.\nThe agent economy is in the same moment Stripe found in 2010.\n",
    content: `Before Stripe, accepting payments online was technically painful, fragmented, and trust-broken. Merchants existed. Buyers existed. The infrastructure layer that made the transaction trustworthy at scale didn't. Stripe built it — and became a $95B company without owning a single product being sold.
The agent economy is in the same moment Stripe found in 2010.

When we set out to build an evaluation framework for AI agents, we had to make a choice: measure everything, or measure what matters.

We chose the latter.

Performance tells you if the agent actually does what it claims. Speed tells you if it does it fast enough for your use case. Cost tells you if it's sustainable at scale.

Every other metric is either derived from these three or is noise that distracts from decision-making.

This isn't a limitation — it's a feature. By constraining our evaluation to these three dimensions, we force clarity. Teams don't get lost in dashboards with dozens of metrics. They get a clear signal.

We've seen teams cut their agent evaluation time from weeks to hours using this framework. That's the power of focus.`,
  },
];
