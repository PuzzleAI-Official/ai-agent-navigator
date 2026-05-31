import { motion } from "framer-motion";
import { ArrowLeft, Mail } from "lucide-react";

const Maintenance = () => {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-6">
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
        className="max-w-[520px] w-full text-center"
      >
        <div className="w-12 h-[2px] bg-accent/30 mx-auto mb-8" style={{ transform: "skewX(-20deg)" }} />

        <h1 className="font-display text-[clamp(2rem,4vw,3rem)] leading-[1.1] tracking-[-0.02em] text-foreground mb-6">
          Under Maintenance
        </h1>

        <p className="text-muted-foreground text-[15px] leading-relaxed mb-4">
          The Playground is currently under further development and is not open to public access yet.
        </p>
        <p className="text-muted-foreground text-[15px] leading-relaxed mb-12">
          However, we'd love to give you an individual demo upon request.
        </p>

        <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
          <a
            href="mailto:info@puzzleai.us?subject=Demo%20Request"
            className="group inline-flex items-center gap-2.5 bg-foreground text-background px-7 py-3.5 font-grotesk font-semibold text-[12px] uppercase tracking-[0.08em] transition-all duration-300 hover:shadow-[0_8px_24px_-8px_hsl(220_20%_50%/0.3)]"
          >
            <Mail size={14} />
            <span>Request a Demo</span>
          </a>

          <a
            href="/"
            className="group inline-flex items-center gap-2.5 border border-border text-foreground px-7 py-3.5 font-grotesk font-semibold text-[12px] uppercase tracking-[0.08em] transition-all duration-300 hover:border-accent/40"
          >
            <ArrowLeft size={14} />
            <span>Return Home</span>
          </a>
        </div>
      </motion.div>
    </div>
  );
};

export default Maintenance;
