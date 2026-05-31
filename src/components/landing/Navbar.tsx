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
        {/* Logo with diagonal slash motif */}
        <a href="#" className="group flex items-center gap-0">
          <span className="font-grotesk font-bold text-[20px] tracking-[-0.03em] text-foreground">
            puzzle
          </span>
          <span className="font-grotesk font-bold text-[20px] tracking-[-0.03em] text-accent">
            ai
          </span>
          <span className="font-grotesk font-bold text-[20px] text-accent">
            .
          </span>
        </a>

        <div className="hidden md:flex items-center gap-10">
          {[
            { label: "Home", href: "/" },
            { label: "Feature", href: "/feature" },
            { label: "API", href: "/alpha-api" },
            { label: "About", href: "/about" },
          ].map((item) => (
            <a
              key={item.label}
              href={item.href}
              className="relative text-[13px] font-grotesk font-medium tracking-[-0.01em] text-muted-foreground hover:text-foreground transition-colors duration-300 group"
            >
              {item.label}
              <span className="absolute -bottom-1 left-0 w-0 h-[2px] bg-accent group-hover:w-full transition-all duration-300 origin-left" style={{ transform: "skewX(-12deg)" }} />
            </a>
          ))}
        </div>

        <a
          href="/playground"
          className="group text-[13px] font-grotesk font-semibold tracking-[-0.01em] text-foreground border border-foreground px-5 py-2.5 hover:bg-foreground hover:text-background transition-all duration-300 flex items-center gap-2 relative overflow-hidden"
        >
          <span className="relative z-10">Get started</span>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="relative z-10 opacity-0 -translate-x-2 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-300">
            <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </a>
      </div>
    </motion.nav>
  );
};

export default Navbar;
