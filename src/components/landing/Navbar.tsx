import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "lucide-react";

const Navbar = () => {
  return (
    <motion.nav
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5 }}
      className="fixed top-0 left-0 right-0 z-50 bg-background/70 backdrop-blur-xl border-b border-border/50"
    >
      <div className="container flex h-16 items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="h-8 w-8 rounded-lg bg-gradient-brand flex items-center justify-center shadow-md">
            <span className="text-primary-foreground font-heading font-bold text-sm">P</span>
          </div>
          <span className="font-heading font-semibold text-lg tracking-tight text-foreground">
            PuzzleAI
          </span>
        </div>

        <div className="hidden md:flex items-center gap-8">
          {["How it works", "Why PuzzleAI", "Pricing"].map((item) => (
            <a
              key={item}
              href={`#${item.toLowerCase().replace(/\s/g, "-")}`}
              className="text-sm text-muted-foreground hover:text-foreground transition-colors duration-200"
            >
              {item}
            </a>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
            Sign in
          </Button>
          <Button size="sm" className="bg-gradient-brand text-primary-foreground hover:opacity-90 transition-opacity font-medium shadow-md">
            Get started
          </Button>
        </div>
      </div>
    </motion.nav>
  );
};

export default Navbar;
