import { motion } from "framer-motion";

const CTASection = () => {
  return (
    <section id="start" className="py-40 border-t border-border">
      <div className="max-w-[1400px] mx-auto px-8 text-center">
        <motion.div
          initial={{ opacity: 0, y: 40 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.8 }}
        >
          <h2 className="font-display text-[clamp(3rem,7vw,6.5rem)] leading-[0.95] tracking-[-0.03em] mb-10">
            Stop researching.
            <br />
            Start <span className="italic">deciding</span>.
          </h2>
          <a
            href="#"
            className="inline-flex items-center gap-3 bg-foreground text-background px-10 py-5 font-mono text-[13px] uppercase tracking-[0.12em] hover:opacity-80 transition-opacity"
          >
            Get started — it's free
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </a>
        </motion.div>
      </div>
    </section>
  );
};

export default CTASection;
