'use client';

/**
 * Top Pages table component — Google Analytics style.
 * Shows the most visited pages ranked by total events.
 * Each page expands to show feature-level breakdown with inPagePct bars.
 * Clean blue/gray palette, no rank badges.
 */

import React, { memo } from 'react';
import { ExternalLink, ChevronDown, ChevronRight } from 'lucide-react';
import ChartContainer from './ChartContainer';
import { TopPage, PageFeature } from '@/types';

interface TopPagesProps {
  data: TopPage[];
}

function TopPages({ data }: TopPagesProps) {
  const baseUrl = process.env.NEXT_PUBLIC_NEXABANK_URL || 'http://localhost:3002';
  const [expandedPage, setExpandedPage] = React.useState<string | null>(null);

  const maxEvents = Math.max(...data.map(d => Number(d.totalEvents) || 0), 1);

  return (
    <ChartContainer title="Top Pages" id="top-pages">
      <table className="w-full px-10 mt-2">
        <thead>
          <tr className="border-b border-gray-100">
            <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-wider pb-2 pl-1">
              Page URL
            </th>
            <th className="text-right text-xs font-semibold text-gray-400 uppercase tracking-wider pb-2 pr-1">
              Events
            </th>
          </tr>
        </thead>
        <tbody>
          {data.map((page, index) => {
            if (!page.pageUrl) return null;
            const pageUrl = page.pageUrl;
            const isExpanded = expandedPage === pageUrl;
            const features = page.features || [];
            const isClickable = pageUrl.startsWith('/');

            // Use backend comparisonPct if available, else compute proportionally
            const barPct = page.comparisonPct != null && page.comparisonPct > 0
              ? page.comparisonPct
              : Math.round((page.totalEvents / maxEvents) * 100);

            return (
              <React.Fragment key={pageUrl}>
                <tr
                  className={`border-b border-gray-50 last:border-0 transition-colors group ${
                    isClickable ? 'hover:bg-gray-50/50' : ''
                  }`}
                >
                  <td className="py-2.5 pl-1">
                    <div className="flex items-center gap-2">
                      {/* Row number */}
                      <span className="text-xs text-gray-400 font-medium w-4 flex-shrink-0 tabular-nums">
                        {index + 1}
                      </span>

                      {/* Expand toggle */}
                      {features.length > 0 ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setExpandedPage(isExpanded ? null : pageUrl);
                          }}
                          className="p-0.5 hover:bg-gray-100 rounded transition-colors cursor-pointer flex-shrink-0"
                          aria-label={isExpanded ? 'Collapse features' : 'Expand features'}
                        >
                          {isExpanded
                            ? <ChevronDown className="w-3.5 h-3.5 text-gray-400" />
                            : <ChevronRight className="w-3.5 h-3.5 text-gray-400" />
                          }
                        </button>
                      ) : (
                        <span className="w-5 flex-shrink-0" />
                      )}

                      {/* URL + external link */}
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span
                          className={`text-sm font-medium transition-colors truncate ${
                            isClickable
                              ? 'text-[#1a73e8] hover:underline cursor-pointer'
                              : 'text-gray-500'
                          }`}
                          onClick={() => {
                            if (isClickable) window.open(`${baseUrl}${pageUrl}`, '_blank');
                          }}
                          title={pageUrl}
                        >
                          {pageUrl}
                        </span>
                        {isClickable && (
                          <ExternalLink
                            className="w-3 h-3 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer flex-shrink-0"
                            onClick={() => window.open(`${baseUrl}${pageUrl}`, '_blank')}
                          />
                        )}
                      </div>

                      {/* Feature count badge */}
                      {features.length > 0 && (
                        <span className="text-[10px] text-gray-400 hidden sm:inline tabular-nums flex-shrink-0">
                          {features.length} features
                        </span>
                      )}
                    </div>
                  </td>

                  {/* Right: bar + count */}
                  <td className="py-2.5 pr-1 text-right">
                    <div className="flex items-center justify-end gap-2">
                      {/* GA-style blue bar */}
                      <div className="w-20 h-1.5 bg-gray-100 rounded-full overflow-hidden hidden sm:block">
                        <div
                          className="h-full rounded-full bg-[#1a73e8] transition-all duration-500"
                          style={{ width: `${Math.min(barPct, 100)}%` }}
                        />
                      </div>
                      <span className="text-sm font-semibold text-gray-800 min-w-[48px] text-right tabular-nums">
                        {typeof page.totalEvents === 'number'
                          ? page.totalEvents.toLocaleString()
                          : page.totalEvents}
                      </span>
                    </div>
                  </td>
                </tr>

                {/* ── Expanded feature breakdown ── */}
                {isExpanded && features.length > 0 && (
                  <tr>
                    <td colSpan={2} className="pb-2">
                      <div className="ml-9 pl-3 border-l-2 border-[#1a73e8]/20 py-1 space-y-2">
                        <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-300 mb-1">
                          Features · % within page
                        </p>
                        {features.map((item: PageFeature) => {
                          const pct = Math.min(item.inPagePct ?? 0, 100);
                          return (
                            <div key={item.feature} className="flex flex-col gap-0.5">
                              <div className="flex items-center justify-between text-[12px] pr-2">
                                <div className="flex items-center gap-1.5 min-w-0">
                                  <span className="w-1.5 h-1.5 rounded-full bg-[#1a73e8]/50 flex-shrink-0" />
                                  <span className="text-gray-700 font-medium truncate" title={item.feature}>
                                    {item.displayName || item.feature}
                                  </span>
                                </div>
                                <div className="flex items-center gap-2 flex-shrink-0">
                                  <span className="text-[10px] text-gray-400">{pct.toFixed(1)}%</span>
                                  <span className="text-xs text-gray-600 font-semibold min-w-[40px] text-right tabular-nums">
                                    {Number(item.count).toLocaleString()}
                                  </span>
                                </div>
                              </div>
                              {/* inPagePct bar */}
                              <div className="ml-3 h-1 bg-gray-100 rounded-full overflow-hidden">
                                <div
                                  className="h-full rounded-full bg-[#4285F4] transition-all duration-500"
                                  style={{ width: `${pct}%`, opacity: 0.6 }}
                                />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </ChartContainer>
  );
}

export default memo(TopPages);
