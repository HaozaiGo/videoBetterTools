import { Link } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { getBootstrap } from "../api/client";
import { ToolIcon } from "../lib/tool-icons";

export function ToolsPage() {
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });

  return (
    <>
      <div className="page-intro">
      </div>
      {data.categories.map((category) => (
        <section className="tool-section" key={category.id}>
          <h2>{category.name}</h2>
          <div className="tool-grid">
            {data.tools
              .filter((tool) => tool.category === category.id)
              .map((tool) => (
                <article className="tool-card" key={tool.slug}>
                  <ToolIcon type={tool.icon} />
                  <div className="tool-copy">
                    <h3>{tool.name}</h3>
                    <p>{tool.summary}</p>
                    <div className="tool-meta">
                      <span>
                        {tool.pricing.mode === "image"
                          ? `每张 ${tool.pricing.unitCredits} 积分起`
                          : `每 ${tool.pricing.unitSeconds} 秒 ${tool.pricing.unitCredits} 积分起`}
                      </span>
                      <span>{tool.status === "online" ? "已上线" : "即将上线"}</span>
                    </div>
                    {tool.status === "online" ? (
                      <Link className="primary" to={tool.route}>
                        去使用 ›
                      </Link>
                    ) : (
                      <button className="ghost" disabled>
                        即将上线
                      </button>
                    )}
                  </div>
                </article>
              ))}
          </div>
        </section>
      ))}
    </>
  );
}
