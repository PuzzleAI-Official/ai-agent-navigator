import { motion, useScroll, useTransform } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import PuzzleBackground from "./PuzzleBackground";

const PLACEHOLDER_EXAMPLES = [
  "I need an AI to summarize legal documents",
  "I need an AI to generate marketing copy",
  "I need an AI to review my code",
];

const useTypingPlaceholder = (examples: string[], typingSpeed = 60, deletingSpeed = 35, pauseDuration = 2000) => {
  const [placeholder, setPlaceholder] = useState("");
  const [exampleIndex, setExampleIndex] = useState(0);
  const [isTyping, setIsTyping] = useState(true);
  const [charIndex, setCharIndex] = useState(0);
  const [isPaused, setIsPaused] = useState(false);

  useEffect(() => {
    const current = examples[exampleIndex];
    if (isPaused) {
      const timeout = setTimeout(() => {
        setIsPaused(false);
        setIsTyping(false);
      }, pauseDuration);
      return () => clearTimeout(timeout);
    }
    if (isTyping) {
      if (charIndex < current.length) {
        const timeout = setTimeout(() => {
          setPlaceholder(current.slice(0, charIndex + 1));
          setCharIndex(charIndex + 1);
        }, typingSpeed);
        return () => clearTimeout(timeout);
      } else {
        setIsPaused(true);
      }
    } else {
      if (charIndex > 0) {
        const timeout = setTimeout(() => {
          setPlaceholder(current.slice(0, charIndex - 1));
          setCharIndex(charIndex - 1);
        }, deletingSpeed);
        return () => clearTimeout(timeout);
      } else {
        setExampleIndex((exampleIndex + 1) % examples.length);
        setIsTyping(true);
      }
    }
  }, [charIndex, isTyping, isPaused, exampleIndex, examples, typingSpeed, deletingSpeed, pauseDuration]);

  return placeholder;
};

/* Scrolling ticker rows of AI tool names — clean, editorial feel */
const TICKER_ROWS = [
  ["GPT-4o", "Claude 3.5", "Gemini Pro", "Mistral", "Llama 3", "Cohere", "DeepSeek", "Perplexity", "Jasper", "Writer"],
  ["HuggingFace", "Stability AI", "ElevenLabs", "Synthesia", "Bland AI", "Vapi", "Retell AI", "Ada", "Intercom AI", "Drift"],
  ["Salesforce AI", "Notion AI", "Cursor", "Codeium", "Tabnine", "LangChain", "CrewAI", "AutoGPT", "Relevance AI", "Replicate"],
  ["Anthropic", "Midjourney", "Fixie", "BabyAGI", "Groq", "Together AI", "Fireworks", "Modal", "RunPod", "Cerebras"],
];

const TickerBackground = () => {
  return (
    <div className="overflow-hidden pointer-events-none flex flex-col gap-5 opacity-[0.12]">
      {TICKER_ROWS.map((row, rowIndex) => {
        const direction = rowIndex % 2 === 0 ? "left" : "right";
        const speed = 40 + rowIndex * 8;
        const items = [...row, ...row, ...row]; // Triple for seamless loop
        
        return (
          <div key={rowIndex} className="relative whitespace-nowrap">
            <motion.div
              className="inline-flex gap-8"
              animate={{
                x: direction === "left" ? ["0%", "-33.33%"] : ["-33.33%", "0%"],
              }}
              transition={{
                duration: speed,
                repeat: Infinity,
                ease: "linear",
              }}
            >
              {items.map((name, i) => (
                <span
                  key={`${name}-${i}`}
                  className="font-grotesk font-medium text-[13px] md:text-[15px] tracking-[0.05em] uppercase text-foreground"
                >
                  {name}
                </span>
              ))}
            </motion.div>
          </div>
        );
      })}
    </div>
  );
};

const Hero = () => {
  const [chatInput, setChatInput] = useState("");
  const navigate = useNavigate();
  const animatedPlaceholder = useTypingPlaceholder(PLACEHOLDER_EXAMPLES);
  const section2Ref = useRef<HTMLDivElement>(null);

  const { scrollYProgress } = useScroll({
    target: section2Ref,
    offset: ["start end", "end start"],
  });

  const transitionOpacity = useTransform(scrollYProgress, [0.6, 0.85], [0, 1]);
  const transitionY = useTransform(scrollYProgress, [0.6, 0.85], [40, 0]);

  const handleChatSubmit = () => {
    if (!chatInput.trim()) return;
    navigate("/playground", { state: { initialMessage: chatInput } });
  };

  return (
    <div className="relative" style={{ height: "340vh" }}>
      {/* Shared background for both sections */}
      <div className="absolute inset-0">
        <div className="sticky top-0 h-screen" style={{
          background: "linear-gradient(170deg, hsl(36 50% 91%) 0%, hsl(38 40% 94%) 30%, hsl(40 33% 97%) 55%, hsl(38 30% 95%) 100%)"
        }} />
      </div>
      <div className="absolute top-0 left-[10%] w-[80%] h-[75vh] bg-[radial-gradient(ellipse_at_50%_40%,hsl(33_55%_85%/0.55),transparent_65%)] pointer-events-none" />
      <div className="absolute top-[5vh] left-[20%] w-[60%] h-[55vh] bg-[radial-gradient(ellipse_at_50%_35%,hsl(260_20%_90%/0.18),transparent_55%)] pointer-events-none" />
      <div
        className="absolute inset-0 opacity-[0.015] pointer-events-none"
        style={{
          backgroundImage: `repeating-linear-gradient(-45deg, transparent, transparent 120px, hsl(var(--foreground)) 120px, hsl(var(--foreground)) 121px)`,
        }}
      />

      {/* Puzzle pieces layer */}
      <PuzzleBackground />

      {/* SECTION 1: Chat prompt — stays at top */}
      <div className="relative z-20 h-screen flex flex-col items-center justify-center">
        <div className="w-full max-w-[720px] px-8 flex flex-col items-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="text-center mb-10"
          >
            <h1 className="font-display leading-[1] tracking-[-0.03em] text-foreground mb-4 font-serif font-normal text-5xl">
              FIND YOUR LAST PUZZLE
            </h1>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.7 }}
            className="w-full"
          >
            <div className="relative bg-[hsl(220_20%_12%)] backdrop-blur-md border border-[hsl(220_15%_20%)] shadow-[0_8px_40px_-12px_hsl(220_30%_8%/0.5)] transition-all duration-300 focus-within:shadow-[0_12px_50px_-10px_hsl(220_30%_8%/0.6)] focus-within:border-[hsl(220_15%_28%)]">
              <textarea
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleChatSubmit();
                  }
                }}
                placeholder={animatedPlaceholder + "│"}
                rows={3}
                className="w-full bg-transparent px-6 py-5 text-[14px] text-[hsl(220_15%_90%)] placeholder:text-[hsl(220_10%_50%)] outline-none resize-none font-sans leading-relaxed rounded-none"
              />
              <div className="flex items-center justify-between px-5 pb-4">
                <span className="text-[11px] font-grotesk text-[hsl(220_10%_40%)] tracking-wide">
                  Press Enter to start
                </span>
                <button
                  onClick={handleChatSubmit}
                  className="group flex items-center gap-2 bg-[hsl(220_15%_90%)] text-[hsl(220_20%_12%)] px-5 py-2 font-grotesk font-semibold text-[11px] uppercase tracking-[0.08em] hover:bg-white hover:shadow-[0_8px_24px_-8px_hsl(220_20%_50%/0.3)] transition-all duration-300"
                >
                  <span>Start</span>
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
                    <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
            </div>
          </motion.div>
        </div>
      </div>

      {/* SECTION 2: Extended "The AI hiring platform" with ticker */}
      <div ref={section2Ref} className="relative z-20 min-h-[160vh]" style={{ marginTop: "80vh" }}>
        <div className="min-h-screen flex flex-col justify-end pb-16 md:pb-24 relative">
          <div className="max-w-[1400px] mx-auto w-full px-8 relative z-10">
            <motion.div
              initial={{ opacity: 0 }}
              whileInView={{ opacity: 1 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.8 }}
              className="flex items-center gap-4 mb-8"
            >
              <div className="w-8 h-[2px] bg-accent/30" style={{ transform: "skewX(-20deg)" }} />
              <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
                The AI hiring platform
              </span>
            </motion.div>

            <motion.h2
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 1.2, ease: [0.16, 1, 0.3, 1] }}
              className="font-display text-[clamp(2.8rem,6.5vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground"
            >
              We help you find the <span className="italic text-gradient">right</span> AI.
            </motion.h2>

            <motion.p
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-50px" }}
              transition={{ duration: 0.7, delay: 0.15 }}
              className="mt-6 text-[15px] md:text-[17px] text-muted-foreground max-w-[620px] leading-[1.8]"
            >
              Thousands of AI services and agents emerge every week — and update just as fast. 
              New models, new frameworks, new promises. Keeping up is a full-time job, and picking 
              the wrong one costs you months.
            </motion.p>

            <motion.p
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-50px" }}
              transition={{ duration: 0.7, delay: 0.3 }}
              className="mt-4 text-[15px] md:text-[17px] text-muted-foreground max-w-[620px] leading-[1.8]"
            >
              It's nearly impossible to understand what you <span className="italic text-foreground/80">really</span> need 
              for your specific workflow and project — until now.
            </motion.p>
          </div>

          {/* Ticker rows — positioned below the text */}
          <motion.div
            initial={{ opacity: 0 }}
            whileInView={{ opacity: 1 }}
            viewport={{ once: true }}
            transition={{ duration: 1, delay: 0.5 }}
            className="mt-16 md:mt-24"
          >
            <TickerBackground />
          </motion.div>
        </div>

        {/* Transition bridge toward Live Evaluation */}
        <div className="relative pb-20 md:pb-32">
          <div className="max-w-[1400px] mx-auto w-full px-8">
            {/* Connecting line */}
            <motion.div
              className="w-px h-24 bg-gradient-to-b from-accent/0 via-accent/30 to-accent/0 mx-auto mb-16"
              initial={{ scaleY: 0, opacity: 0 }}
              whileInView={{ scaleY: 1, opacity: 1 }}
              viewport={{ once: true }}
              transition={{ duration: 0.8 }}
              style={{ transformOrigin: "top" }}
            />

            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 0.8 }}
              className="text-center max-w-2xl mx-auto"
            >
              <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/40 block mb-6">
                Our approach
              </span>
              <p className="font-display text-[clamp(1.6rem,3.5vw,3rem)] leading-[1.1] tracking-[-0.02em] text-foreground">
                Describe your workflow.{" "}
                <span className="italic text-muted-foreground">
                  We test every AI solution against your real use cases
                </span>{" "}
                — and deliver three verdicts.
              </p>
              <div className="flex items-center justify-center gap-8 mt-10">
                {["Performance", "Speed", "Cost"].map((metric, i) => (
                  <motion.span
                    key={metric}
                    initial={{ opacity: 0, y: 10 }}
                    whileInView={{ opacity: 1, y: 0 }}
                    viewport={{ once: true }}
                    transition={{ delay: 0.3 + i * 0.15 }}
                    className="font-mono text-[11px] uppercase tracking-[0.2em] text-accent/60 border-b border-accent/20 pb-1"
                  >
                    {metric}
                  </motion.span>
                ))}
              </div>
            </motion.div>

            {/* Arrow pointing down to next section */}
            <motion.div
              className="flex justify-center mt-16"
              initial={{ opacity: 0 }}
              whileInView={{ opacity: 1 }}
              viewport={{ once: true }}
              transition={{ delay: 0.6 }}
            >
              <motion.svg
                width="20"
                height="32"
                viewBox="0 0 20 32"
                fill="none"
                className="text-accent/30"
                animate={{ y: [0, 6, 0] }}
                transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
              >
                <path d="M10 0V28M10 28L2 20M10 28L18 20" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </motion.svg>
            </motion.div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Hero;
