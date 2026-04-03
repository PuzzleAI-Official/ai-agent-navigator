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
    <section ref={containerRef} className="py-32 overflow-hidden">
      <div className="max-w-[1400px] mx-auto px-8 mb-16">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
        >
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground block mb-6">
            Companies
          </span>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-3xl">
            Trusted by teams who
            <br />
            <span className="italic">refuse to guess.</span>
          </h2>
        </motion.div>
      </div>

      {/* Horizontal scroll testimonials */}
      <motion.div style={{ x }} className="flex gap-6 px-8">
        {testimonials.map((t, i) => (
          <motion.div
            key={t.name}
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6, delay: i * 0.15 }}
            className="flex-shrink-0 w-[480px] border border-border p-10 group hover:bg-secondary/30 transition-colors duration-500 cursor-default"
          >
            {/* Large metric */}
            <div className="mb-8">
              <span className="font-display text-5xl md:text-6xl tracking-tight text-accent">{t.metric}</span>
              <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground ml-3">{t.metricLabel}</span>
            </div>

            <blockquote className="text-foreground/80 leading-relaxed mb-8 text-[15px]">
              "{t.quote}"
            </blockquote>

            <div className="flex items-center justify-between pt-6 border-t border-border">
              <div>
                <div className="font-grotesk font-medium text-sm">{t.name}</div>
                <div className="font-mono text-[10px] text-muted-foreground tracking-wider">{t.role}</div>
              </div>
              <span className="font-grotesk font-semibold text-foreground/40 text-sm group-hover:text-foreground/60 transition-colors">{t.company}</span>
            </div>
          </motion.div>
        ))}
      </motion.div>
    </section>
  );
};

export default Testimonials;
