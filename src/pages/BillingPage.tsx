import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { getBootstrap, recharge } from "../api/client";
import { formatCredits, formatDate, ledgerAmount } from "../lib/format";
import type { BootstrapState, LedgerEntry } from "../types";

const columnHelper = createColumnHelper<LedgerEntry>();

export function BillingPage() {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const rechargeMutation = useMutation({
    mutationFn: recharge,
    onSuccess: (payload) => queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state),
  });

  const columns = [
    columnHelper.accessor("title", {
      header: "流水",
      cell: ({ row }) => (
        <>
          <span>{row.original.title}</span>
          <em>{formatDate(row.original.createdAt)}</em>
        </>
      ),
    }),
    columnHelper.display({
      id: "amount",
      header: "金额",
      cell: ({ row }) => <strong>{ledgerAmount(row.original)}</strong>,
    }),
  ];

  const table = useReactTable({
    data: data.ledger,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <section className="billing-layout">
      <div className="billing-main">
        <div className="page-head">
          <div>
            <h1>充值与消耗</h1>
            <p>当前仍为模拟充值入口，后续接入真实支付时保留同一账务流水。</p>
          </div>
        </div>
        <div className="wallet-hero">
          <span>当前可用余额</span>
          <strong>{formatCredits(data.account.availableCredits)}</strong>
          <p>总余额 {formatCredits(data.account.credits)} · 冻结 {formatCredits(data.account.frozenCredits)}</p>
          <button className="primary" onClick={() => rechargeMutation.mutate(100)} disabled={rechargeMutation.isPending}>
            立即充值 ›
          </button>
        </div>
        <div className="pricing-cards">
          {[100, 300, 800].map((credits) => (
            <button className="price-card" key={credits} onClick={() => rechargeMutation.mutate(credits)} disabled={rechargeMutation.isPending}>
              <strong>{credits} 积分</strong>
              <span>模拟充值，后端立即入账</span>
            </button>
          ))}
        </div>
      </div>
      <div className="panel ledger-panel">
        <h2>流水</h2>
        <div className="ledger-table-scroll">
          <table className="ledger-table">
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.length ? (
                table.getRowModel().rows.map((row) => (
                  <tr key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={columns.length} className="empty">
                    暂无消耗记录。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
