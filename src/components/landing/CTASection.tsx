import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

const CTASection = () => {
  const sectionRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: sectionRef,
    offset: ["start end", "end start"],
  });
  const scale = useTransform(scrollYProgress, [0, 0.5], [0.92, 1]);
  const opacity = useTransform(scrollYProgress, [0, 0.3], [0, 1]);

  return (
    <section ref={sectionRef} id="start" className="py-20 md:py-40 relative overflow-hidden">
      <div className="absolute inset-0 pointer-events-none">
        <motion.div
          className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[800px] h-[800px] bg-accent/[0.02] rounded-full blur-[150px]"
          style={{ scale }}
        />
        {/* Diagonal grid background */}
        <div
          className="absolute inset-0 opacity-[0.02]"
          style={{
            backgroundImage: `repeating-linear-gradient(
              -45deg,
              transparent,
              transparent 80px,
              hsl(var(--foreground)) 80px,
              hsl(var(--foreground)) 81px
            )`,
          }}
        />
      </div>

      <motion.div style={{ scale, opacity }} className="max-w-[1400px] mx-auto px-8">
        <div className="border border-border p-12 md:p-20 relative bg-background/50 backdrop-blur-sm">
          {/* Diagonal corner accents — signature */}
          <div className="absolute top-0 left-0 w-12 h-12 overflow-hidden">
            <div className="absolute w-[1px] h-20 bg-accent/40 origin-top-left" style={{ transform: "rotate(45deg)" }} />
          </div>
          <div className="absolute top-0 right-0 w-12 h-12 overflow-hidden">
            <div className="absolute right-0 w-[1px] h-20 bg-accent/40 origin-top-right" style={{ transform: "rotate(-45deg)" }} />
          </div>
          <div className="absolute bottom-0 left-0 w-12 h-12 overflow-hidden">
            <div className="absolute bottom-0 w-[1px] h-20 bg-accent/40 origin-bottom-left" style={{ transform: "rotate(-45deg)" }} />
          </div>
          <div className="absolute bottom-0 right-0 w-12 h-12 overflow-hidden">
            <div className="absolute bottom-0 right-0 w-[1px] h-20 bg-accent/40 origin-bottom-right" style={{ transform: "rotate(45deg)" }} />
          </div>

          <div className="text-center">
            <motion.div
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.8 }}
            >
              <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/40 block mb-8">
                Ready?
              </span>
              <h2 className="font-display text-[clamp(3rem,7vw,7rem)] leading-[0.9] tracking-[-0.03em] mb-6">
                Stop researching.
                <br />
                Start <span className="italic text-gradient">deciding</span>.
              </h2>
              <p className="text-muted-foreground max-w-lg mx-auto mb-12 leading-relaxed">
                Describe your workflow in plain language. Get performance, speed, and cost
                verdicts on 200+ AI solutions in under five minutes.
              </p>
              <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
                <a
                  href="/playground"
                  className="group relative inline-flex items-center gap-3 bg-foreground text-background px-10 py-5 font-grotesk font-semibold text-[13px] uppercase tracking-[0.08em] overflow-hidden transition-all duration-300 hover:shadow-[0_12px_40px_-12px_hsl(215_20%_50%/0.4)] shimmer-hover"
                >
                  <span className="relative z-10">Get started — it's free</span>
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1">
                    <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </a>
                <a href="#" className="font-grotesk font-medium text-[13px] tracking-[-0.01em] text-muted-foreground hover:text-foreground transition-colors px-6 py-5">
                  Book a demo →
                </a>
              </div>
            </motion.div>
          </div>
        </div>
      </motion.div>
    </section>
  );
};

export default CTASection;
