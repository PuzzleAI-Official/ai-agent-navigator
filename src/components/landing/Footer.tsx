import { motion } from "framer-motion";

const Footer = () => {
  return (
    <footer className="border-t border-border">
      <div className="max-w-[1400px] mx-auto px-8 py-16">
        <div className="grid md:grid-cols-12 gap-12 mb-16">
          {/* Brand column */}
          <div className="md:col-span-4">
            <div className="flex items-baseline gap-0 mb-4">
              <span className="font-display text-[20px] italic tracking-tight text-foreground">Puzzle</span>
              <span className="font-grotesk font-bold text-[20px] tracking-tight text-accent">AI</span>
            </div>
            <p className="text-muted-foreground text-sm leading-relaxed max-w-xs">
              The first agent-to-agent hiring platform. Find the right AI for your work.
            </p>
          </div>

          {/* Link columns */}
          {[
            { title: "Product", links: ["How it works", "Companies", "Pricing", "API"] },
            { title: "Company", links: ["About", "Blog", "Careers", "Contact"] },
            { title: "Connect", links: ["Twitter", "LinkedIn", "GitHub", "Discord"] },
          ].map((col) => (
            <div key={col.title} className="md:col-span-2">
              <h4 className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">{col.title}</h4>
              <ul className="space-y-2.5">
                {col.links.map((link) => (
                  <li key={link}>
                    <a href="#" className="text-sm text-foreground/60 hover:text-foreground transition-colors duration-200">
                      {link}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        {/* Bottom bar */}
        <div className="pt-8 border-t border-border flex flex-col md:flex-row items-center justify-between gap-4">
          <span className="font-mono text-[10px] text-muted-foreground tracking-wider">
            © 2026 PuzzleAI Inc. All rights reserved.
          </span>
          <div className="flex gap-6">
            {["Privacy", "Terms", "Security"].map((item) => (
              <a key={item} href="#" className="font-mono text-[10px] text-muted-foreground hover:text-foreground transition-colors tracking-wider">
                {item}
              </a>
            ))}
          </div>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
