import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import PasswordGate from "@/components/PasswordGate";
import Index from "./pages/Index.tsx";
import NotFound from "./pages/NotFound.tsx";
import Maintenance from "./pages/Maintenance.tsx";
import Feature from "./pages/Feature.tsx";
import ApiDocs from "./pages/ApiDocs.tsx";
import AlphaApiDocs from "./pages/AlphaApiDocs.tsx";
import AlphaSdkDocs from "./pages/AlphaSdkDocs.tsx";
import About from "./pages/About.tsx";
import BlogPost from "./pages/BlogPost.tsx";

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <PasswordGate>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Index />} />
            <Route path="/playground" element={<Maintenance />} />
            <Route path="/feature" element={<Feature />} />
            <Route path="/api" element={<ApiDocs />} />
            <Route path="/alpha-api" element={<AlphaApiDocs />} />
            <Route path="/alpha-sdk" element={<AlphaSdkDocs />} />
            <Route path="/about" element={<About />} />
            <Route path="/about/:slug" element={<BlogPost />} />
            {/* ADD ALL CUSTOM ROUTES ABOVE THE CATCH-ALL "*" ROUTE */}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </BrowserRouter>
      </PasswordGate>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
