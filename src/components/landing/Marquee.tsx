import { motion } from "framer-motion";

const logos = [
  "Anthropic", "OpenAI", "Google", "Mistral", "Cohere",
  "Replicate", "HuggingFace", "AWS Bedrock", "Azure AI", "Groq",
  "Perplexity", "Claude", "Gemini", "LangChain", "CrewAI",
];

const Marquee = () => {
  return (
    <section className="py-20 overflow-hidden relative">
      {/* Diagonal stripe accent — PuzzleAI signature */}
      <div className="absolute top-0 left-0 right-0 h-px bg-border" />
      <div className="absolute bottom-0 left-0 right-0 h-px bg-border" />
      
      {/* Diagonal hatching in corners */}
      <div className="absolute top-4 left-8 opacity-[0.06]">
        <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
          <line x1="0" y1="40" x2="40" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="30" x2="30" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="20" x2="20" y2="0" stroke="currentColor" strokeWidth="1"/>
        </svg>
      </div>
      <div className="absolute top-4 right-8 opacity-[0.06]" style={{ transform: "scaleX(-1)" }}>
        <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
          <line x1="0" y1="40" x2="40" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="30" x2="30" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="20" x2="20" y2="0" stroke="currentColor" strokeWidth="1"/>
        </svg>
      </div>

      <div className="mb-10 text-center relative">
        <span className="font-grotesk font-medium text-[13px] tracking-[-0.01em] text-muted-foreground/60">
          Evaluating 200+ AI solutions across every category
        </span>
      </div>
      <div className="relative">
        <div className="absolute left-0 top-0 bottom-0 w-32 bg-gradient-to-r from-background to-transparent z-10" />
        <div className="absolute right-0 top-0 bottom-0 w-32 bg-gradient-to-l from-background to-transparent z-10" />

        <div className="flex animate-marquee whitespace-nowrap">
          {[...logos, ...logos].map((name, i) => (
            <span
              key={i}
              className="mx-12 text-lg font-grotesk font-semibold text-foreground/10 hover:text-accent/40 transition-colors duration-500 cursor-default select-none"
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
