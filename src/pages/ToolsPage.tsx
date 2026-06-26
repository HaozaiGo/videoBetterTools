import { Link, useNavigate } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { getBootstrap } from "../api/client";
import { formatCredits, statusLabel, taskProgressDisplay } from "../lib/format";
import { ToolIcon } from "../lib/tool-icons";

export function ToolsPage() {
  const navigate = useNavigate();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const [command, setCommand] = useState("");
  const [commandNotice, setCommandNotice] = useState("");
  const recentTasks = data.tasks.slice(0, 3);
  const visibleTools = data.tools.filter((tool) => tool.status !== "disabled" && tool.slug !== "subtitle-translate-workflow");
  const videoTools = visibleTools.filter((tool) => tool.category === "video");
  const imageTools = visibleTools.filter((tool) => tool.category === "image");
  const featuredTools = visibleTools.filter((tool) => tool.status === "online").slice(0, 4);
  const handleCommandSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedCommand = command.trim().replace(/\s+/g, "");
    if (normalizedCommand === "内部使用1688") {
      setCommandNotice("");
      navigate({ to: "/internal/batch-workflow" });
      return;
    }
    setCommandNotice("未识别到内部口令，已为你打开视频去字幕工具。");
    navigate({ to: "/tools/video/$toolSlug", params: { toolSlug: "remove-subtitle" } });
  };

  return (
    <div className="launcher-page">
      <section className="launcher-hero">
        <p className="eyebrow">AI VIDEO COMMAND CENTER</p>
        <h1>今天要处理什么视频？</h1>
        <p>搜索工具或输入目标，系统推荐最短路径。上传、GPU任务、扣费和结果都在一个工作台里完成。</p>
        <form className="command-search" onSubmit={handleCommandSubmit}>
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="视频处理命令"
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="例如：把这段口播视频去字幕后转成 2K"
          />
          <button className="primary" type="submit">
            开始 ›
          </button>
        </form>
        {commandNotice ? <p className="command-notice">{commandNotice}</p> : null}
      </section>

      <section className="quick-tools" aria-label="推荐工具">
        {featuredTools.map((tool) => (
          <Link className="quick-tool-card" key={tool.slug} to={tool.route}>
            <ToolIcon type={tool.icon} />
            <div>
              <strong>{tool.name}</strong>
              <span>{tool.pricing.mode === "image" ? `每张 ${tool.pricing.unitCredits} 积分起` : `每 ${tool.pricing.unitSeconds} 秒 ${tool.pricing.unitCredits} 积分起`}</span>
            </div>
          </Link>
        ))}
      </section>

      <section className="launcher-grid">
        <div className="tool-library">
          <div className="section-head">
            <div>
              <h2>视频工具</h2>
              <p>优先展示当前商业主链路：去字幕、去水印、超分与任务队列。</p>
            </div>
          </div>
          <div className="tool-card-grid">
            {videoTools.map((tool) => (
              <article className="tool-card" key={tool.slug}>
                <ToolIcon type={tool.icon} />
                <div className="tool-copy">
                  <div className="tool-title-row">
                    <h3>{tool.name}</h3>
                    <span className={`tool-state ${tool.status}`}>{tool.status === "online" ? "已上线" : "即将上线"}</span>
                  </div>
                  <p>{tool.summary}</p>
                  <div className="tool-meta">
                    <span>{tool.pricing.mode === "image" ? `每张 ${tool.pricing.unitCredits} 积分起` : `每 ${tool.pricing.unitSeconds} 秒 ${tool.pricing.unitCredits} 积分起`}</span>
                    <span>{tool.provider?.includes("video") ? "GPU/Worker" : "供应商"}</span>
                    {tool.category === "video" && tool.status === "online" ? <span>支持批处理</span> : null}
                  </div>
                  {tool.status === "online" ? (
                    <Link className="primary compact" to={tool.route}>
                      去使用 ›
                    </Link>
                  ) : (
                    <button className="ghost compact" disabled>
                      即将上线
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
          <div className="section-head image-tools-head">
            <div>
              <h2>图片工具</h2>
              <p>保留图片消除、换背景入口，后续可并入同一资产库。</p>
            </div>
          </div>
          <div className="tool-card-grid image-grid">
            {imageTools.map((tool) => (
              <article className="tool-card small" key={tool.slug}>
                <ToolIcon type={tool.icon} />
                <div className="tool-copy">
                  <h3>{tool.name}</h3>
                  <p>{tool.summary}</p>
                  <Link className="primary compact" to={tool.route}>
                    去使用 ›
                  </Link>
                </div>
              </article>
            ))}
          </div>
        </div>

        <aside className="launcher-side">
          <div className="side-metric dark">
            <span>当前余额</span>
            <strong>{formatCredits(data.account.availableCredits)}</strong>
            <p>失败任务自动释放冻结积分。</p>
          </div>
          <div className="recent-card">
            <div className="section-head slim">
              <h2>最近任务</h2>
              <Link to="/tasks">全部 ›</Link>
            </div>
            {recentTasks.length ? (
              recentTasks.map((task) => {
                const tool = data.tools.find((item) => item.slug === task.toolSlug);
                const progress = taskProgressDisplay(task);
                return (
                  <Link className="recent-item" key={task.id} to="/tasks">
                    <div>
                      <strong>{tool?.name || task.toolSlug}</strong>
                      <span>{progress.stage || statusLabel(task.status)}</span>
                    </div>
                    <em>{task.status === "succeeded" ? "查看" : `${progress.percent}%`}</em>
                  </Link>
                );
              })
            ) : (
              <p className="empty-lite">还没有任务，从左侧工具开始。</p>
            )}
          </div>
        </aside>
      </section>
    </div>
  );
}
