import { motion } from "framer-motion";
import { useEffect, useRef, useState, useCallback } from "react";
import heroMetal from "@/assets/hero-metal.png";

const Hero = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [mousePos, setMousePos] = useState({ x: 0.5, y: 0.5 });

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    setMousePos({
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    });
  }, []);

  const offsetX = (mousePos.x - 0.5) * 25;
  const offsetY = (mousePos.y - 0.5) * 18;

  // Typing effect
  const fullText = "Describe your workflow. We test every AI solution against your real use cases and deliver three verdicts: performance, speed, cost.";
  const [displayed, setDisplayed] = useState("");
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setStarted(true), 2200);
    return () => clearTimeout(timeout);
  }, []);

  useEffect(() => {
    if (!started) return;
    let i = 0;
    const interval = setInterval(() => {
      setDisplayed(fullText.slice(0, i + 1));
      i++;
      if (i >= fullText.length) clearInterval(interval);
    }, 18);
    return () => clearInterval(interval);
  }, [started]);

  return (
    <section
      ref={containerRef}
      onMouseMove={handleMouseMove}
      className="relative min-h-screen overflow-hidden"
    >
      {/* ── Warm gradient background ── */}
      <div className="absolute inset-0" style={{
        background: "linear-gradient(165deg, hsl(38 45% 93%) 0%, hsl(40 33% 97%) 35%, hsl(42 30% 96%) 60%, hsl(38 35% 94%) 100%)"
      }} />
      <div className="absolute top-0 left-[15%] w-[70%] h-[70%] bg-[radial-gradient(ellipse_at_50%_30%,hsl(35_50%_88%/0.5),transparent_65%)] pointer-events-none" />
      <div className="absolute top-[5%] left-[25%] w-[50%] h-[50%] bg-[radial-gradient(ellipse_at_50%_40%,hsl(270_25%_90%/0.15),transparent_55%)] pointer-events-none" />
      <div className="absolute bottom-0 left-0 w-full h-[30%] bg-gradient-to-t from-background to-transparent pointer-events-none" />

      {/* ── Content: vertical stack ── */}
      <div className="relative z-10 min-h-screen flex flex-col pt-24">
        <div className="max-w-[1400px] mx-auto w-full px-8">
          
          {/* Eyebrow */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3, duration: 0.8 }}
            className="flex items-center gap-4 mb-6"
          >
            <motion.div
              className="w-12 h-px bg-gradient-to-r from-accent to-accent/10"
              initial={{ scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={{ delay: 0.5, duration: 1 }}
              style={{ transformOrigin: "left" }}
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.3em] text-muted-foreground">
              The AI hiring platform
            </span>
          </motion.div>
        </div>

        {/* ── Sculpture — centered, dramatic, ON TOP of headline ── */}
        <div className="flex justify-center items-center py-4 md:py-6 relative">
          {/* Glow behind sculpture */}
          <motion.div
            initial={{ opacity: 0, scale: 0.5 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.3, duration: 2.5, ease: [0.16, 1, 0.3, 1] }}
            className="absolute w-[70vw] h-[50vw] max-w-[900px] max-h-[600px]"
            style={{
              background: "radial-gradient(ellipse, hsl(35 50% 85% / 0.25) 0%, hsl(270 25% 88% / 0.1) 40%, transparent 70%)",
              transform: `translate(${offsetX * 0.3}px, ${offsetY * 0.3}px)`,
            }}
          />

          {/* The sculpture */}
          <motion.img
            src={heroMetal}
            alt=""
            width={1920}
            height={1080}
            initial={{ opacity: 0, scale: 0.8, y: 40 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: 2, delay: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative z-10 w-[75vw] md:w-[50vw] lg:w-[42vw] max-w-[700px] h-auto"
            style={{
              transform: `translate(${offsetX}px, ${offsetY}px)`,
              transition: "transform 0.15s ease-out",
              filter: "drop-shadow(0 40px 80px rgba(80, 60, 40, 0.15)) drop-shadow(0 15px 30px rgba(60, 50, 40, 0.1))",
            }}
          />

          {/* Floating particles */}
          {[...Array(8)].map((_, i) => {
            const angle = (i / 8) * Math.PI * 2;
            const radius = 150 + (i % 3) * 60;
            const size = i % 3 === 0 ? 3 : 1.5;
            return (
              <motion.div
                key={i}
                className="absolute rounded-full"
                style={{
                  width: size,
                  height: size,
                  left: `calc(50% + ${Math.cos(angle) * radius}px)`,
                  top: `calc(50% + ${Math.sin(angle) * radius}px)`,
                  background: i % 3 === 0 ? "hsl(35, 45%, 65%)" : "hsl(225, 35%, 70%)",
                }}
                animate={{
                  y: [0, -15 - i * 2, 0],
                  opacity: [0.1, 0.4, 0.1],
                  scale: [1, 1.8, 1],
                }}
                transition={{
                  duration: 3 + i * 0.4,
                  repeat: Infinity,
                  delay: i * 0.3,
                  ease: "easeInOut",
                }}
              />
            );
          })}
        </div>

        {/* ── Full-width headline BELOW the sculpture ── */}
        <div className="max-w-[1400px] mx-auto w-full px-8 mt-2">
          <motion.h1
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1.2, delay: 0.6, ease: [0.16, 1, 0.3, 1] }}
            className="font-display text-[clamp(2.8rem,6.5vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground"
          >
            We help you find the <span className="italic text-gradient">right</span> AI.
          </motion.h1>

          <motion.p
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 1.1 }}
            className="mt-6 text-[15px] md:text-[16px] text-muted-foreground max-w-[580px] leading-[1.75]"
          >
            {displayed}
            {started && displayed.length < fullText.length && (
              <span className="animate-pulse text-accent ml-0.5">|</span>
            )}
          </motion.p>

          {/* CTAs */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 1.4 }}
            className="mt-10 flex flex-col sm:flex-row items-start sm:items-center gap-5"
          >
            <a
              href="#start"
              className="group relative inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[12px] uppercase tracking-[0.15em] overflow-hidden transition-all duration-500 hover:shadow-[0_20px_60px_-15px_hsl(225_45%_42%/0.3)]"
            >
              <span className="relative z-10">Try PuzzleAI</span>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1.5">
                <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              <div className="absolute inset-0 bg-accent/30 translate-y-full group-hover:translate-y-0 transition-transform duration-500" />
            </a>
            <a
              href="#how-it-works"
              className="group font-mono text-[12px] uppercase tracking-[0.15em] text-muted-foreground hover:text-foreground transition-colors duration-300 flex items-center gap-2"
            >
              <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-accent transition-colors duration-300">Learn more</span>
            </a>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default Hero;
