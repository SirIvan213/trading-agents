import { Header } from "./components/Header";
import { DecisionDetail } from "./pages/DecisionDetail";
import { History } from "./pages/History";
import { HowItWorks } from "./pages/HowItWorks";
import { NotFound, Placeholder } from "./pages/Placeholder";
import { useRoute } from "./router";

export function App() {
  const route = useRoute();
  return (
    <div className="app">
      <Header route={route} />
      {route.page === "about" && <HowItWorks />}
      {route.page === "live" && <Placeholder page="live" />}
      {route.page === "history" && <History />}
      {route.page === "decision" && <DecisionDetail key={route.id} runId={route.id} />}
      {route.page === "not_found" && <NotFound hash={route.hash} />}
    </div>
  );
}
