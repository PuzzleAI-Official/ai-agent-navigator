import { motion } from "framer-motion";
import heroImage from "@/assets/hero-abstract.jpg";

const Hero = () => {
  return (
    <section className="relative min-h-screen flex flex-col">
      {/* Hero image — full bleed */}
      <div className="relative w-full h-[70vh] overflow-hidden">
        <motion.img
          src={heroImage}
          alt=""
          width={1920}
          height={1080}
          className="w-full h-full object-cover"
          initial={{ scale: 1.1, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 1.2, ease: "easeOut" }}
        />
        {/* Gradient fade to background */}
        <div className="absolute inset-0 bg-gradient-to-b from-background/30 via-transparent to-background" />
        
        {/* Floating badge */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.6, duration: 0.6 }}
          className="absolute top-32 left-8 md:left-16"
        >
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-foreground/60">
            The AI hiring platform
          </span>
        </motion.div>
      </div>

      {/* Headline section — overlapping the image */}
      <div className="max-w-[1400px] mx-auto w-full px-8 -mt-32 relative z-10">
        <motion.h1
          initial={{ opacity: 0, y: 40 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.8, delay: 0.3 }}
          className="font-display text-[clamp(3rem,8vw,7.5rem)] leading-[0.95] tracking-[-0.03em] text-foreground max-w-4xl"
        >
          We help you find the{" "}
          <span className="relative inline-block">
            <span className="italic">right</span>
            {/* Sequoia-style hand-drawn circle */}
            <svg
              viewBox="0 0 120 50"
              className="absolute -inset-x-3 -inset-y-2 w-[calc(100%+24px)] h-[calc(100%+16px)]"
              fill="none"
            >
              <ellipse
                cx="60"
                cy="25"
                rx="55"
                ry="20"
                stroke="hsl(160 60% 42%)"
                strokeWidth="2"
                className="draw-circle"
                transform="rotate(-2 60 25)"
              />
            </svg>
          </span>{" "}
          AI.
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.6 }}
          className="mt-10 text-lg md:text-xl text-muted-foreground max-w-xl leading-relaxed"
        >
          Describe your workflow. We test every AI solution against your real
          use cases and deliver three verdicts: performance, speed, cost.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.9 }}
          className="mt-10 flex items-center gap-6"
        >
          <a
            href="#start"
            className="inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[13px] uppercase tracking-[0.12em] hover:opacity-80 transition-opacity"
          >
            Try PuzzleAI
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="transition-transform group-hover:translate-x-1">
              <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </a>
          <a
            href="#how-it-works"
            className="font-mono text-[13px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors border-b border-muted-foreground/30 pb-0.5"
          >
            Learn more
          </a>
        </motion.div>
      </div>
    </section>
  );
};

export default Hero;
