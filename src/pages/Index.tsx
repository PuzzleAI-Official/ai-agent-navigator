import Navbar from "@/components/landing/Navbar";
import Hero from "@/components/landing/Hero";
import Marquee from "@/components/landing/Marquee";

import ProductDemo from "@/components/landing/ProductDemo";
import Testimonials from "@/components/landing/Testimonials";

import CTASection from "@/components/landing/CTASection";
import Footer from "@/components/landing/Footer";

const Index = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Hero />
      <Marquee />
      
      <ProductDemo />
      <Testimonials />
      <WhySection />
      <CTASection />
      <Footer />
    </div>
  );
};

export default Index;
