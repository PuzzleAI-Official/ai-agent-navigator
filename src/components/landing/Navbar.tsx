import { motion } from "framer-motion";
import { useEffect, useState } from "react";

const Navbar = () => {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 50);
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <motion.nav
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.6, delay: 0.2 }}
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-500 ${
        scrolled ? "bg-background/90 backdrop-blur-xl border-b border-border" : ""
      }`}
    >
      <div className="max-w-[1400px] mx-auto flex h-20 items-center justify-between px-8">
        <span className="font-grotesk font-semibold text-xl tracking-tight text-foreground">
          PuzzleAI
        </span>

        <div className="hidden md:flex items-center gap-10">
          {["Companies", "How it works", "About"].map((item) => (
            <a
              key={item}
              href={`#${item.toLowerCase().replace(/\s/g, "-")}`}
              className="text-[13px] font-mono uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors duration-300"
            >
              {item}
            </a>
          ))}
        </div>

        <a
          href="#start"
          className="text-[13px] font-mono uppercase tracking-[0.12em] text-foreground border border-foreground px-5 py-2.5 hover:bg-foreground hover:text-background transition-all duration-300"
        >
          Get started
        </a>
      </div>
    </motion.nav>
  );
};

export default Navbar;
