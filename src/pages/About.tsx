import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import { format } from "date-fns";
import heroSilk from "@/assets/hero-silk.png";
import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";
import { articles } from "@/data/articles";

const About = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Mission statement with silk background */}
      <section className="relative overflow-hidden">
        <div className="absolute inset-0">
          <img
            src={heroSilk}
            alt=""
            className="absolute inset-0 w-full h-full object-cover"
          />
          <div className="absolute inset-0" style={{
            background: "linear-gradient(170deg, hsl(36 50% 91% / 0.85) 0%, hsl(38 40% 94% / 0.8) 30%, hsl(40 33% 97% / 0.75) 55%, hsl(38 30% 95% / 0.7) 100%)"
          }} />
        </div>

        <div className="relative max-w-[1400px] mx-auto px-8 pt-32 pb-16 md:pt-40 md:pb-20">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7 }}
            className="max-w-[720px]"
          >
            <div className="mb-10" />

            <h1 className="font-display text-[clamp(2rem,4vw,3rem)] leading-[1.15] tracking-[-0.02em] mb-6">
              Built by agent builders,{" "}
              <span className="italic text-muted-foreground">for everyone.</span>
            </h1>

            <p className="text-muted-foreground text-[16px] leading-[1.85] font-sans max-w-[600px]">
              PuzzleAI is founded by agent builders with a single focus: developing
              infrastructure that benefits everyone in the era of AI agents. We
              believe evaluation should be simple, transparent, and accessible.
            </p>
          </motion.div>
        </div>
      </section>

      {/* Articles / Blog */}
      <section className="max-w-[1400px] mx-auto px-8 pt-8 pb-16 md:pt-12 md:pb-24">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          <div className="flex items-center gap-6 mb-10">
            <h2 className="font-display text-[clamp(1.75rem,3vw,2.5rem)] tracking-[-0.02em] text-foreground/85">
              Blogs
            </h2>
            <div className="flex-1 h-[1px] bg-border" />
          </div>
        </motion.div>

        <div className="space-y-0">
          {articles.map((article, i) => (
            <motion.div
              key={article.slug}
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.3 + i * 0.1 }}
            >
              <Link
                to={`/about/${article.slug}`}
                className="group block border-b border-border py-8 md:py-10 hover:bg-muted/30 -mx-8 px-8 transition-colors duration-300"
              >
                <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-4">
                  <div className="flex-1 max-w-[600px]">
                    <h2 className="font-display text-xl md:text-2xl leading-tight mb-2 group-hover:translate-x-1 transition-transform duration-300">
                      {article.title}
                    </h2>
                    <p className="text-muted-foreground text-sm leading-relaxed">
                      {article.excerpt}
                    </p>
                  </div>

                  <div className="flex items-center gap-3 md:text-right md:flex-shrink-0">
                    <div>
                      <p className="font-grotesk text-xs text-muted-foreground">
                        {article.author}
                      </p>
                      <p className="font-grotesk text-[11px] text-muted-foreground/50">
                        {format(new Date(article.date), "MMM d, yyyy")}
                      </p>
                    </div>
                  </div>
                </div>

                <span className="inline-block mt-4 font-grotesk text-xs uppercase tracking-[0.15em] text-accent/60 group-hover:text-accent transition-colors duration-300">
                  Read →
                </span>
              </Link>
            </motion.div>
          ))}
        </div>
      </section>

      <Footer />
    </div>
  );
};

export default About;