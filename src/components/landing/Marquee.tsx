import { motion } from "framer-motion";

const logos = [
  "Anthropic", "OpenAI", "Google", "Mistral", "Cohere",
  "Replicate", "HuggingFace", "AWS Bedrock", "Azure AI", "Groq",
  "Perplexity", "Claude", "Gemini", "LangChain", "CrewAI",
];

const Marquee = () => {
  return (
    <section className="py-16 border-y border-border overflow-hidden">
      <div className="mb-8 text-center">
        <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
          200+ AI solutions evaluated
        </span>
      </div>
      <div className="relative">
        <div className="flex animate-marquee whitespace-nowrap">
          {[...logos, ...logos].map((name, i) => (
            <span
              key={i}
              className="mx-10 text-lg font-grotesk font-medium text-foreground/20 hover:text-foreground/60 transition-colors duration-500 cursor-default select-none"
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
