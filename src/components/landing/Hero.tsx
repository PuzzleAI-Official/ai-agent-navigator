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

  const offsetX = (mousePos.x - 0.5) * 30;
  const offsetY = (mousePos.y - 0.5) * 22;

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
      {/* ── Layered warm gradient background ── */}
      <div className="absolute inset-0" style={{
        background: "linear-gradient(170deg, hsl(36 50% 91%) 0%, hsl(38 40% 94%) 30%, hsl(40 33% 97%) 55%, hsl(38 30% 95%) 100%)"
      }} />
      
      {/* Warm golden glow centered behind sculpture */}
      <div className="absolute top-[-5%] left-[10%] w-[80%] h-[75%] bg-[radial-gradient(ellipse_at_50%_40%,hsl(33_55%_85%/0.55),transparent_65%)] pointer-events-none" />
      
      {/* Subtle cool undertone for depth */}
      <div className="absolute top-[5%] left-[20%] w-[60%] h-[55%] bg-[radial-gradient(ellipse_at_50%_35%,hsl(260_20%_90%/0.18),transparent_55%)] pointer-events-none" />
      
      {/* Bottom fade to base */}
      <div className="absolute bottom-0 left-0 w-full h-[35%] bg-gradient-to-t from-background to-transparent pointer-events-none" />

      {/* ── Sculpture zone — overlapping into text for natural merge ── */}
      <div className="absolute top-0 left-0 w-full h-[75vh] md:h-[70vh] flex justify-center items-center pointer-events-none">
        {/* Multi-layered glow halos */}
        <motion.div
          initial={{ opacity: 0, scale: 0.4 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.2, duration: 3, ease: [0.16, 1, 0.3, 1] }}
          className="absolute w-[90vw] h-[60vw] max-w-[1100px] max-h-[700px]"
          style={{
            background: "radial-gradient(ellipse, hsl(33 55% 83% / 0.35) 0%, hsl(33 45% 88% / 0.15) 35%, hsl(260 20% 90% / 0.06) 55%, transparent 75%)",
            transform: `translate(${offsetX * 0.15}px, ${offsetY * 0.15}px)`,
          }}
        />

        {/* Inner bright core glow */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.8, duration: 2 }}
          className="absolute w-[40vw] h-[30vw] max-w-[550px] max-h-[400px]"
          style={{
            background: "radial-gradient(ellipse, hsl(35 60% 90% / 0.3) 0%, transparent 65%)",
            transform: `translate(${offsetX * 0.2}px, ${offsetY * 0.2}px)`,
          }}
        />

        {/* The sculpture — BIGGER, overlapping into the text zone */}
        <motion.img
          src={heroMetal}
          alt=""
          width={1920}
          height={1080}
          initial={{ opacity: 0, scale: 0.75, y: 50 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          transition={{ duration: 2.2, delay: 0.15, ease: [0.16, 1, 0.3, 1] }}
          className="relative z-10 w-[90vw] md:w-[60vw] lg:w-[52vw] max-w-[850px] h-auto"
          style={{
            transform: `translate(${offsetX}px, ${offsetY}px)`,
            transition: "transform 0.12s ease-out",
            filter: "drop-shadow(0 50px 100px rgba(80, 55, 30, 0.18)) drop-shadow(0 20px 40px rgba(60, 45, 30, 0.12)) drop-shadow(0 5px 15px rgba(40, 30, 20, 0.06))",
          }}
        />

        {/* Floating luminous particles */}
        {[...Array(10)].map((_, i) => {
          const angle = (i / 10) * Math.PI * 2;
          const radius = 180 + (i % 4) * 55;
          const size = i % 4 === 0 ? 3.5 : i % 2 === 0 ? 2 : 1;
          return (
            <motion.div
              key={i}
              className="absolute rounded-full"
              style={{
                width: size,
                height: size,
                left: `calc(50% + ${Math.cos(angle) * radius}px)`,
                top: `calc(48% + ${Math.sin(angle) * radius}px)`,
                background: i % 4 === 0 
                  ? "hsl(35, 55%, 60%)" 
                  : i % 3 === 0 
                    ? "hsl(225, 40%, 68%)" 
                    : "hsl(280, 25%, 75%)",
              }}
              animate={{
                y: [0, -18 - i * 2, 0],
                opacity: [0.08, 0.35, 0.08],
                scale: [1, 2, 1],
              }}
              transition={{
                duration: 3.5 + i * 0.35,
                repeat: Infinity,
                delay: i * 0.25,
                ease: "easeInOut",
              }}
            />
          );
        })}
      </div>

      {/* ── Content layer ── */}
      <div className="relative z-20 min-h-screen flex flex-col justify-end pb-12 md:pb-16">
        <div className="max-w-[1400px] mx-auto w-full px-8">
          
          {/* Eyebrow — top-left, above everything */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.4, duration: 0.8 }}
            className="absolute top-24 left-8 flex items-center gap-4"
          >
            <motion.div
              className="w-12 h-px bg-gradient-to-r from-accent to-accent/10"
              initial={{ scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={{ delay: 0.6, duration: 1 }}
              style={{ transformOrigin: "left" }}
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.3em] text-muted-foreground">
              The AI hiring platform
            </span>
          </motion.div>

          {/* Headline — full width, sits below/overlapping the sculpture */}
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
            className="mt-6 text-[15px] md:text-[16px] text-muted-foreground max-w-[560px] leading-[1.75]"
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
