import { useParams, Link } from "react-router-dom";
import { motion } from "framer-motion";
import { format } from "date-fns";
import { ArrowLeft } from "lucide-react";
import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";
import { articles } from "@/data/articles";

const BlogPost = () => {
  const { slug } = useParams<{ slug: string }>();
  const article = articles.find((a) => a.slug === slug);

  if (!article) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <div className="max-w-[1400px] mx-auto px-8 pt-32 pb-24">
          <p className="text-muted-foreground">Article not found.</p>
          <Link to="/about" className="font-grotesk text-xs uppercase tracking-[0.15em] text-accent mt-4 inline-block">
            ← Back to About
          </Link>
        </div>
        <Footer />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <article className="max-w-[1400px] mx-auto px-8 pt-32 pb-24 md:pt-40 md:pb-32">
        {/* Back link */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
        >
          <Link
            to="/about"
            className="inline-flex items-center gap-1.5 font-grotesk text-xs uppercase tracking-[0.15em] text-accent/60 hover:text-accent transition-colors mb-12"
          >
            <ArrowLeft className="w-3 h-3" />
            Back
          </Link>
        </motion.div>

        {/* Letter card */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.1 }}
          className="max-w-[680px]"
        >
          {/* Header */}
          <div className="border-b border-border pb-8 mb-10">
            <h1 className="font-display text-[clamp(2rem,4.5vw,3.2rem)] leading-[1.1] tracking-[-0.02em] mb-6">
              {article.title}
            </h1>

            <div className="flex items-center gap-3">
              {article.authorImage ? (
                <img
                  src={article.authorImage}
                  alt={article.author}
                  className="w-10 h-10 object-cover"
                />
              ) : (
                <div className="w-10 h-10 bg-muted flex items-center justify-center">
                  <span className="font-grotesk text-xs font-semibold text-muted-foreground">
                    {article.author
                      .split(" ")
                      .map((n) => n[0])
                      .join("")}
                  </span>
                </div>
              )}
              <div>
                <p className="font-grotesk text-sm font-medium">{article.author}</p>
                <p className="font-grotesk text-[11px] text-muted-foreground/60">
                  {article.authorRole} · {format(new Date(article.date), "MMMM d, yyyy")}
                </p>
              </div>
            </div>
          </div>

          {/* Article body */}
          <div className="space-y-6 text-foreground/80 text-[16px] leading-[1.9] font-sans">
            {article.content.split("\n\n").map((paragraph, i) => {
              const trimmed = paragraph.trim();
              if (!trimmed) return null;
              // Detect section headers (short lines without periods, typically under 60 chars)
              const isHeader = trimmed.length < 80 && !trimmed.includes('.') && !trimmed.includes('?') && !trimmed.includes('—') && trimmed === trimmed.replace(/\n/g, '');
              if (isHeader) {
                return <h2 key={i} className="font-display md:text-2xl tracking-[-0.02em] text-foreground mt-4 text-2xl">— {trimmed} —</h2>;
              }
              return <p key={i} className="pl-6">{trimmed}</p>;
            })}
          </div>
        </motion.div>
      </article>

      <Footer />
    </div>
  );
};

export default BlogPost;
