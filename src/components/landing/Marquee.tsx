import { motion } from "framer-motion";

const logos = [
  "Anthropic", "OpenAI", "Google", "Mistral", "Cohere",
  "Replicate", "HuggingFace", "AWS Bedrock", "Azure AI", "Groq",
  "Perplexity", "Claude", "Gemini", "LangChain", "CrewAI",
];

const Marquee = () => {
  return (
    <section className="py-16 border-y border-border overflow-hidden relative">
      {/* Subtle background */}
      <div className="absolute inset-0 bg-gradient-to-r from-card/30 via-transparent to-card/30" />

      <div className="mb-8 text-center relative">
        <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
          200+ AI solutions evaluated
        </span>
      </div>
      <div className="relative">
        {/* Edge fades */}
        <div className="absolute left-0 top-0 bottom-0 w-24 bg-gradient-to-r from-background to-transparent z-10" />
        <div className="absolute right-0 top-0 bottom-0 w-24 bg-gradient-to-l from-background to-transparent z-10" />

        <div className="flex animate-marquee whitespace-nowrap">
          {[...logos, ...logos].map((name, i) => (
            <span
              key={i}
              className="mx-10 text-lg font-grotesk font-medium text-foreground/15 hover:text-foreground/50 transition-colors duration-500 cursor-default select-none"
            >
              {name}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
};

export default Marquee;
