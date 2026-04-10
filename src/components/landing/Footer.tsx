const Footer = () => {
  return (
    <footer className="border-t border-border relative">
      {/* Diagonal hatching in footer corner */}
      <div className="absolute top-6 right-8 opacity-[0.04]">
        <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
          <line x1="0" y1="32" x2="32" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="22" x2="22" y2="0" stroke="currentColor" strokeWidth="1"/>
          <line x1="0" y1="12" x2="12" y2="0" stroke="currentColor" strokeWidth="1"/>
        </svg>
      </div>

      <div className="max-w-[1400px] mx-auto px-8 py-16">
        <div className="grid md:grid-cols-12 gap-12 mb-16">
          <div className="md:col-span-4">
            <div className="flex items-baseline gap-0 mb-4">
              <span className="font-grotesk font-bold text-[18px] tracking-[-0.03em] text-foreground">puzzle</span>
              <span className="font-grotesk font-bold text-[18px] tracking-[-0.03em] text-accent">ai</span>
              <span className="font-grotesk font-bold text-[18px] text-accent">.</span>
            </div>
            <p className="text-muted-foreground text-sm leading-relaxed max-w-xs">
              Smarter AI decisions
            </p>
          </div>

          <div className="md:col-span-2">
            <h4 className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">Product</h4>
            <ul className="space-y-2.5">
              <li><a href="/feature" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">Feature</a></li>
              <li><a href="/api" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">API</a></li>
            </ul>
          </div>

          <div className="md:col-span-2">
            <h4 className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">Company</h4>
            <ul className="space-y-2.5">
              <li><a href="/about" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">About</a></li>
              <li><a href="mailto:info@puzzleai.us" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">Contact</a></li>
            </ul>
          </div>

          <div className="md:col-span-2">
            <h4 className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">Connect</h4>
            <ul className="space-y-2.5">
              <li><a href="#" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">Twitter</a></li>
              <li><a href="#" className="text-sm text-foreground/60 hover:text-accent transition-colors duration-200">LinkedIn</a></li>
            </ul>
          </div>
        </div>

        <div className="pt-8 border-t border-border flex flex-col md:flex-row items-center justify-between gap-4">
          <span className="font-grotesk text-[10px] text-muted-foreground tracking-wider">
            © 2026 PuzzleAI Inc. All rights reserved.
          </span>
          <div className="flex gap-6">
            {["Privacy", "Terms", "Security"].map((item) => (
              <a key={item} href="#" className="font-grotesk text-[10px] text-muted-foreground hover:text-accent transition-colors tracking-wider">
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
