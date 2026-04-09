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
    date: "2026-04-02",
    author: "Lianming Hu",
    authorRole: "Founders",
    excerpt:
      "The inspiration for building A2A infrastructure is not coming from nowhere. It is naturally rooted in the work: the team has been building agentic services and found that the current infrastructure workflow is fragmented.",
    content: `The inspiration for building A2A infrastructure is not coming from nowhere. It is naturally rooted in the work: the team has been building agentic services and found that the current infrastructure workflow is fragmented. Someone has to fix it, why don't we start building that? There comes the story of Puzzle's AI selection platform, and the path it builds towards: an A2A future where HR agents serve across the industry.

A future where agents are in production

Agents in production has been a common goal from individuals to enterprises who use AI nowadays. The team has been building agentic services for years while realizing that a future where agents become more or less a personal asset: just like your Siri, Google Workspace, or Microsoft Office—is inevitable. Regardless of what providers and products look like, it is a trend that agent development is becoming crucial and core in product development across major companies.

Looking through the portfolios of past YC W26 batch — over 60% of the companies are developing AI while 41% are focusing on agents. Looking outside into the everyday world, the majority of our work and life has one product or two or even more trying to take over with AI. People are working on pushing towards an agent world.

However, is AI together with agents in production yet? As McKinsey reported in November 2025, 88% of companies claim to have at least one business function involving AI, but less than 7% have reached maturity in fully scaling across the organization. The majority of AI use is still in the piloting stage.

Gaps are to be filled

The process of turning an AI tool or agent into production level is tedious and challenging. With thousands of new tools, agent services, and protocols released monthly, it is a chaotic market for developers and companies to decide on the best candidate for their workflow. It is neither a lack of experience from developers nor inefficiency on the provider side, the misalignment between the understanding of workflow between the provider and the user is natural and inevitable.

Selecting the right tools currently is, unfortunately, often tied to marketing and SEO engineering, even in the AI era. This is not enough: many of the workflow parts do not necessarily require the most powerful underlying models to run. Costs, security, compliance, response time... different users and tasks come with unique requirements and different rankings of preferences. Reading through hundreds of candidates' websites and APIs is overwhelming enough. It is painful for developers and decision makers to make the right decision.

And thus, commitment often comes with uncertainty. Money has already been paid when you realize agent B is better than the one you are using.

A smarter selection

Think of it like a company hiring their employees. A comprehensive screening and interview process is usually used to select the most suitable, rather than the hardest-working, the smartest, or the best college graduate, for the specific workflow of their position inside the organization. The flow of selecting the right AI agents or tools for the job should be similar. However, it is not there yet. The commitment either goes through tedious evaluation or trial and error.

A smart selection should rely majorly on results, not on marketing or others' word of mouth. It is challenging for current AI-powered recommendation methods to select candidates without bias— the recommendations are made by reading and absorbing the public knowledge base on the internet: documentation, reviews, benchmark leaderboards, and online discourse. This means well-marketed tools get recommended more, not necessarily the ones that best fit your workflow. The bias is structural, not a model quality issue. A result-based approach with evaluation against one's actual data and workflow in a sandbox shifts the comparison from reputation to performance. This provides fundamentally objective options, resulting in a smarter selection.

What if the search process could better understand the true needs of the project and its unique workflow, rather than general search with bias?

What if the workflow could truly be tested with its own unique data in a sandbox before committing and going live?

What if the workflow-side agent could act like an HR hiring another agent, based on all of this?

A smarter selection can be extremely helpful in mitigating risk for decision makers choosing their tools and agents. Not just an efficiency tool, but a way to save the money and time cost of unnecessary or wrong tools.

A future where agents decide

A smarter selection and evaluation infrastructure is needed today to help developers and startup owners decide. But more than today: in the era of A2A, it will be a core part of the system when agents start hiring other agents to perform tasks and jobs.

The infrastructure helps answer: how would one agent think like a human HR when considering hiring others? It comes down to multiple aspects:

Needs understanding

There are existing need-matching platforms and tools, but none have gone deep enough either in understanding the client or the tools. The workflow and job task type with its unique needs is a challenging part. Even the users themselves may be uncertain and abstract about their goals and tasks: it would be error-prone to do the searching with existing platforms as passive search. The understanding part must be proactive, always asking for more details.

Data

The evaluation based on data that is uniquely related to the hiring side best evaluates and provides results, minimizing the misalignment on expectations between the two agents. This matches the situation where the questions and tasks are usually given by the interviewers during candidate screening.

Validation

The ultimate judgement for users of certain tools and agents comes post-use. The results stand for themselves, beyond any marketing, and are representative of all the unique needs and considerations. Result-based selection serves the ultimate goal: find the most appropriate candidate for a specific role or workflow.

This is getting popular in both human hiring: many tech companies like Cursor nowadays give real problems to solve, or even a one-week trial to hire, and enterprise AI tool testing, using sandbox services to safely run and test selected tools. This is the cherry we are proposing to add to the cake: selection will be based on true sandbox tests, not just marketing.

A2A

Everything changes once you step into A2A infrastructure. That's why Puzzle has prepared in advance. A2A's request and handling can be reduced to milliseconds compared to traditional agent selection. Identification, payment, data privacy, and compliance—these are high-risk and top challenges to be solved. While considering this ahead of the A2A era may be seen as risky, Puzzle sees these as necessary steps on later stage toward the future.

The risks

Agent and tool vendor compatibility

The A2A and validation features require test-run-as-result architecture. While it is generally easy to manually set up testing, it is neither cost effective nor automatic and fast enough for A2A adoption. The risk exists in vendors' willingness to cooperate. While there may be more standardized APIs or even standardized test environment protocols in the future, the incentive of being promoted on the platform could mitigate this risk in the short term.

Data privacy and compliance

With more details and data we obtain for an efficient selection process, it raises concerns and works naturally against users' willingness to share their business details and test data. While building trust is hard, the risk can be mitigated by partnering with trustworthy sandbox service providers with maximum transparency in the evaluation process. Synthesizing datasets for testing can also minimize the unwillingness to share sensitive data.

Evaluation and post-selection

Different users value different metrics, and given the fact that not every detail could be shared, the risk cannot be completely eliminated. However, mitigating the risk to the point where the service Puzzle provides is still useful and more efficient than humans manually deciding—that will be the first milestone to hit. The mitigation can include customizable metrics in evaluation, and post-selection ongoing observability services to dynamically adjust metric weights per need.

Current landscape

FutureTools — Platform that provides a 4,000+ AI tool database with search, categories, and rankings. Passive discovery with no needs analysis or testing services.

LangChain — An engineering platform and open-source framework developers use to build, test, and deploy reliable AI agents. Functions include observing and evaluating agents, and helping deploy them without infrastructure complexity. It does not provide any agent or service selection—mainly focused on post-selection evaluation for developers who already know what agent they want.

Daytona — Isolated cloud environments for running and testing agents, code, and AI tools in a sandbox environment safely and efficiently. Does not provide direct comparison on multiple candidates, nor selection suggestions or evaluations. However, it can be considered as an important partner along the Puzzle infrastructure as a sandbox testing environment provider.

Braintrust — An AI observability platform helping teams measure, evaluate, and improve AI in production. Provides higher-level information observability to help developers understand the performance of their deployed AI modules. Does not provide selection suggestions or comparison.

n8n — A workflow automation platform that uniquely combines AI capabilities with business process automation, helping developers create multi-step AI agents and automate workflows using visual tools. It is a tool for improving efficiency in connecting agents and AI tools together. It does not provide comparison, selection suggestions, or workflow and needs understanding.

Microsoft Magentic Marketplace (Research Project) — An open-source simulation environment from Microsoft Research where customer-side AI agents interact with business-side agents in a synthetic marketplace. Tested 100 customer agents against 300 business agents. Results have shown agents being overwhelmed by too many selections and options, revealing the current gap in A2A between piloting and production level.

Final notes

The landscape shows a highly fragmented workflow on pre- and post-agent selection. While many have addressed different aspects, there has not been a combined result-as-a-service platform and infrastructure provided yet.

The A2A hiring architecture is ambitious. However, the workflow is already broken and needs to be fixed now. The booming of agents in today's AI era will eventually force a more standardized flow and environment for tasks like this.

Puzzle would take the first shot.`,
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
