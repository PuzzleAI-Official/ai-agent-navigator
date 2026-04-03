import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

const CTASection = () => {
  const sectionRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: sectionRef,
    offset: ["start end", "end start"],
  });
  const scale = useTransform(scrollYProgress, [0, 0.5], [0.9, 1]);
  const opacity = useTransform(scrollYProgress, [0, 0.3], [0, 1]);

  return (
    <section ref={sectionRef} id="start" className="py-20 md:py-40 relative overflow-hidden">
      {/* Background accent elements */}
      <div className="absolute inset-0 pointer-events-none">
        <motion.div
          className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] bg-accent/[0.04] rounded-full blur-[120px]"
          style={{ scale }}
        />
        {/* Grid lines */}
        <div className="absolute inset-0 opacity-[0.03]" style={{
          backgroundImage: "linear-gradient(hsl(var(--foreground)) 1px, transparent 1px), linear-gradient(90deg, hsl(var(--foreground)) 1px, transparent 1px)",
          backgroundSize: "80px 80px",
        }} />
      </div>

      <motion.div style={{ scale, opacity }} className="max-w-[1400px] mx-auto px-8">
        <div className="border border-border p-12 md:p-20 relative">
          {/* Corner marks */}
          <div className="absolute top-0 left-0 w-4 h-4 border-t-2 border-l-2 border-accent" />
          <div className="absolute top-0 right-0 w-4 h-4 border-t-2 border-r-2 border-accent" />
          <div className="absolute bottom-0 left-0 w-4 h-4 border-b-2 border-l-2 border-accent" />
          <div className="absolute bottom-0 right-0 w-4 h-4 border-b-2 border-r-2 border-accent" />

          <div className="text-center">
            <motion.div
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.8 }}
            >
              <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground block mb-8">
                Ready?
              </span>
              <h2 className="font-display text-[clamp(3rem,7vw,7rem)] leading-[0.9] tracking-[-0.03em] mb-6">
                Stop researching.
                <br />
                Start <span className="italic">deciding</span>.
              </h2>
              <p className="text-muted-foreground max-w-lg mx-auto mb-12 leading-relaxed">
                Describe your workflow in plain language. Get performance, speed, and cost
                verdicts on 200+ AI solutions in under five minutes.
              </p>
              <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
                <a
                  href="#"
                  className="group inline-flex items-center gap-3 bg-foreground text-background px-10 py-5 font-mono text-[13px] uppercase tracking-[0.12em] hover:gap-5 transition-all duration-300"
                >
                  Get started — it's free
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
                    <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </a>
                <a
                  href="#"
                  className="font-mono text-[13px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors px-6 py-5"
                >
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
