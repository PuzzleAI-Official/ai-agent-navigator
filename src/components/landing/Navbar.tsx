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
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, delay: 0.2 }}
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-500 ${
        scrolled ? "bg-background/90 backdrop-blur-xl border-b border-border" : ""
      }`}
    >
      <div className="max-w-[1400px] mx-auto flex h-20 items-center justify-between px-8">
        {/* Wordmark — unified elegant treatment */}
        <a href="#" className="group flex items-center gap-0">
          <span className="font-grotesk font-bold text-[20px] tracking-[-0.02em] text-foreground">
            puzzle
          </span>
          <span className="font-grotesk font-bold text-[20px] tracking-[-0.02em] text-accent">
            ai
          </span>
          <span className="font-grotesk font-bold text-[20px] text-accent leading-none ml-[-1px] mb-auto mt-[2px]">
            .
          </span>
        </a>

        <div className="hidden md:flex items-center gap-10">
          {["Companies", "How it works", "About"].map((item) => (
            <a
              key={item}
              href={`#${item.toLowerCase().replace(/\s/g, "-")}`}
              className="relative text-[13px] font-mono uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors duration-300 group"
            >
              {item}
              <span className="absolute -bottom-1 left-0 w-0 h-px bg-accent group-hover:w-full transition-all duration-300" />
            </a>
          ))}
        </div>

        <a
          href="#start"
          className="group text-[13px] font-mono uppercase tracking-[0.12em] text-foreground border border-foreground px-5 py-2.5 hover:bg-foreground hover:text-background transition-all duration-300 flex items-center gap-2"
        >
          Get started
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="opacity-0 -translate-x-2 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-300">
            <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </a>
      </div>
    </motion.nav>
  );
};

export default Navbar;
