import Navbar from "@/components/landing/Navbar";
import Hero from "@/components/landing/Hero";
import HowItWorks from "@/components/landing/HowItWorks";
import WhySection from "@/components/landing/WhySection";
import CTASection from "@/components/landing/CTASection";
import Footer from "@/components/landing/Footer";

const Index = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Hero />
      <HowItWorks />
      <WhySection />
      <CTASection />
      <Footer />
    </div>
  );
};

export default Index;
