import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

const testimonials = [
  {
    quote: "We spent three months evaluating AI solutions for our support team. PuzzleAI gave us the same answer in four minutes — and it was better.",
    name: "Sarah Chen",
    role: "VP Engineering",
    company: "Lattice",
    metric: "4 min",
    metricLabel: "vs 3 months",
  },
  {
    quote: "The testing methodology is what sold us. It's not opinions or benchmarks — it's our actual data, our actual edge cases. No one else does this.",
    name: "Marcus Webb",
    role: "CTO",
    company: "Ramp",
    metric: "$340k",
    metricLabel: "saved annually",
  },
  {
    quote: "We were about to sign a $200k contract with the wrong provider. PuzzleAI showed us a solution that was 40% faster and half the cost.",
    name: "Anya Patel",
    role: "Head of AI",
    company: "Notion",
    metric: "40%",
    metricLabel: "faster solution",
  },
];

const Testimonials = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start end", "end start"],
  });
  const x = useTransform(scrollYProgress, [0, 1], ["5%", "-15%"]);

  return (
    <section ref={containerRef} className="py-32 overflow-hidden relative">
      {/* Subtle gradient transition */}
      <div className="absolute inset-0 bg-gradient-to-b from-background via-card/20 to-background" />

      <div className="max-w-[1400px] mx-auto px-8 mb-16 relative">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
        >
          <div className="flex items-center gap-4 mb-6">
            <motion.div
              className="w-8 h-px bg-accent"
              initial={{ scaleX: 0 }}
              whileInView={{ scaleX: 1 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6 }}
              style={{ transformOrigin: "left" }}
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
              Companies
            </span>
          </div>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-3xl">
            Trusted by teams who
            <br />
            <span className="italic">refuse to guess.</span>
          </h2>
        </motion.div>
      </div>

      <motion.div style={{ x }} className="flex gap-6 px-8 relative">
        {testimonials.map((t, i) => (
          <motion.div
            key={t.name}
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6, delay: i * 0.15 }}
            className="flex-shrink-0 w-[480px] border border-border p-10 group hover:border-accent/30 transition-all duration-500 cursor-default bg-background/60 backdrop-blur-sm relative overflow-hidden"
          >
            {/* Hover accent line */}
            <div className="absolute top-0 left-0 w-0 h-[2px] bg-accent group-hover:w-full transition-all duration-700" />

            <div className="mb-8">
              <span className="font-display text-5xl md:text-6xl tracking-tight text-accent italic">{t.metric}</span>
              <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground ml-3">{t.metricLabel}</span>
            </div>

            <blockquote className="text-foreground/70 leading-relaxed mb-8 text-[15px]">
              "{t.quote}"
            </blockquote>

            <div className="flex items-center justify-between pt-6 border-t border-border">
              <div>
                <div className="font-grotesk font-medium text-sm">{t.name}</div>
                <div className="font-mono text-[10px] text-muted-foreground tracking-wider">{t.role}</div>
              </div>
              <span className="font-grotesk font-semibold text-foreground/30 text-sm group-hover:text-foreground/50 transition-colors">{t.company}</span>
            </div>
          </motion.div>
        ))}
      </motion.div>
    </section>
  );
};

export default Testimonials;
