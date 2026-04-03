import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "lucide-react";

const CTASection = () => {
  return (
    <section className="py-32 relative overflow-hidden">
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-border to-transparent" />

      <div className="container max-w-3xl mx-auto px-4 text-center relative z-10">
        {/* Glowing orb */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[300px] rounded-full bg-primary/[0.04] blur-[100px] pointer-events-none" />

        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
        >
          <h2 className="text-4xl md:text-6xl font-heading font-bold tracking-tight mb-6">
            Find the AI that
            <br />
            <span className="text-gradient">actually works.</span>
          </h2>
          <p className="text-muted-foreground text-lg mb-10 max-w-xl mx-auto font-body">
            Stop wasting time and money on the wrong tools.
            Let PuzzleAI test, compare, and recommend — so you can decide with confidence.
          </p>
          <Button
            size="lg"
            className="bg-gradient-brand text-primary-foreground hover:opacity-90 transition-opacity font-medium text-base px-10 h-13 gap-2 group shadow-lg shadow-primary/20"
          >
            Start for free
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </Button>
        </motion.div>
      </div>
    </section>
  );
};

export default CTASection;
