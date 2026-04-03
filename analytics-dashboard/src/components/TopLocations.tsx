'use client';

/**
 * Top Locations map component.
 * Uses react-simple-maps with a clean, high-density Google Analytics look.
 * Includes a sidebar table with country + continent views.
 */

import { memo, useState, useMemo } from 'react';
import {
  ComposableMap,
  Geographies,
  Geography,
  ZoomableGroup,
} from 'react-simple-maps';
import { Plus, Minus, Maximize2, ExternalLink, Globe, Map } from 'lucide-react';
import ChartContainer from './ChartContainer';
import { LocationData } from '@/types';

interface TopLocationsProps {
  data: LocationData[];
}

const geoUrl = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json";

function TopLocations({ data }: TopLocationsProps) {
  const [position, setPosition] = useState({ coordinates: [0, 20], zoom: 1 });
  const [viewMode, setViewMode] = useState<'country' | 'continent'>('country');

  function handleZoomIn() {
    if (position.zoom >= 8) return;
    setPosition((pos) => ({ ...pos, zoom: pos.zoom * 1.5 }));
  }

  function handleZoomOut() {
    if (position.zoom <= 1) return;
    setPosition((pos) => ({ ...pos, zoom: pos.zoom / 1.5 }));
  }

  function handleMoveEnd(newPosition: { coordinates: [number, number]; zoom: number }) {
    // Check if state actually changed to avoid render loop
    if (newPosition.zoom !== position.zoom || 
        newPosition.coordinates[0] !== position.coordinates[0] || 
        newPosition.coordinates[1] !== position.coordinates[1]) {
      setTimeout(() => setPosition(newPosition), 0);
    }
  }
  // Calculate percentages for the table
  const totalVisits = data.reduce((sum, item) => sum + item.visits, 0);
  const maxVisits = data.length > 0 ? Math.max(...data.map(d => d.visits)) : 1;

  // Aggregate by continent
  const continentData = useMemo(() => {
    const continentMap: Record<string, number> = {};
    for (const item of data) {
      const continent = (item as any).continent || 'Other';
      continentMap[continent] = (continentMap[continent] || 0) + item.visits;
    }
    return Object.entries(continentMap)
      .map(([continent, visits]) => ({ continent, visits }))
      .sort((a, b) => b.visits - a.visits);
  }, [data]);

  const maxContinentVisits = continentData.length > 0 ? Math.max(...continentData.map(d => d.visits)) : 1;

  // Continent colors for visual grouping
  const continentColors: Record<string, string> = {
    'Asia': '#1a73e8',
    'North America': '#1a73e8',
    'Europe': '#1a73e8',
    'South America': '#1a73e8',
    'Africa': '#1a73e8',
    'Oceania': '#1a73e8',
    'Other': '#1a73e8',
  };

  return (
    <ChartContainer title="Geographic Distribution" id="top-locations">
      <div className="flex flex-col lg:flex-row gap-6 mt-4">
        
        {/* Left: Interactive Map */}
        <div className="relative flex-1 h-[400px] bg-gray-50/50 rounded-xl overflow-hidden border border-gray-100 group cursor-move">
          
          {/* Zoom Controls */}
          <div className="absolute right-3 top-3 flex flex-col gap-1.5 z-10">
            <button 
              onClick={handleZoomIn}
              className="p-1.5 bg-white/90 backdrop-blur-sm rounded border border-gray-200 shadow-sm hover:bg-white text-gray-500 hover:text-blue-600 transition-all"
              title="Zoom In"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
            <button 
              onClick={handleZoomOut}
              className="p-1.5 bg-white/90 backdrop-blur-sm rounded border border-gray-200 shadow-sm hover:bg-white text-gray-500 hover:text-blue-600 transition-all"
              title="Zoom Out"
            >
              <Minus className="w-3.5 h-3.5" />
            </button>
            <button 
              onClick={() => setPosition({ coordinates: [0, 20], zoom: 1 })}
              className="p-1.5 bg-white/90 backdrop-blur-sm rounded border border-gray-200 shadow-sm hover:bg-white text-gray-500 hover:text-blue-600 transition-all"
              title="Reset View"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          </div>

          <ComposableMap
            projectionConfig={{ scale: 140 }}
            width={800}
            height={500}
            className="w-full h-full"
          >
            <ZoomableGroup
              zoom={position.zoom}
              center={position.coordinates as [number, number]}
              onMoveEnd={handleMoveEnd}
            >
              <Geographies geography={geoUrl}>
                {({ geographies }) =>
                  geographies.map((geo) => {
                    const countryName = geo.properties.name;
                    const locationData = data.find(d => 
                      d.country === countryName || 
                      (countryName === 'United States of America' && (d.country === 'USA' || d.country === 'United States' || d.country === 'America')) ||
                      (countryName === 'United Kingdom' && d.country === 'UK')
                    );

                    // GA Style Coloring: No data = light grey, Data = shades of blue
                    let fillColor = '#f3f4f6';
                    if (locationData) {
                      if (locationData.visits > 50000) fillColor = '#1a73e8';
                      else if (locationData.visits > 30000) fillColor = '#4285f4';
                      else fillColor = '#8ab4f8';
                    }

                    return (
                      <Geography
                        key={geo.rsmKey}
                        geography={geo}
                        fill={fillColor}
                        stroke="#ffffff"
                        strokeWidth={0.5}
                        style={{
                          default: { outline: 'none' },
                          hover: { fill: locationData ? '#174ea6' : '#e5e7eb', outline: 'none', transition: 'all 200ms' },
                          pressed: { outline: 'none' },
                        }}
                      />
                    );
                  })
                }
              </Geographies>
            </ZoomableGroup>
          </ComposableMap>
        </div>

        {/* Right: Top Nations / Continent Table */}
        <div className="w-full lg:w-80 flex flex-col">
          {/* View Toggle */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-0.5">
              <button
                onClick={() => setViewMode('country')}
                className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium transition-all ${
                  viewMode === 'country' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                <Map className="w-3 h-3" />
                Country
              </button>
              <button
                onClick={() => setViewMode('continent')}
                className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium transition-all ${
                  viewMode === 'continent' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                <Globe className="w-3 h-3" />
                Continent
              </button>
            </div>
            <span className="text-[11px] text-gray-400 font-medium">Total: {totalVisits >= 1000 ? `${(totalVisits / 1000).toFixed(1)}k` : totalVisits}</span>
          </div>
          
          <div className="space-y-1">
            {viewMode === 'country' ? (
              // Country View
              [...data].sort((a, b) => b.visits - a.visits).slice(0, 6).map((item, idx) => (
                <div 
                  key={item.country} 
                  className="flex flex-col p-2 rounded-lg hover:bg-gray-50 transition-colors group cursor-pointer border border-transparent hover:border-gray-100"
                >
                <div className="flex items-center justify-between mb-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-gray-700 group-hover:text-blue-600 transition-colors">{item.country}</span>
                  </div>
                  <span className="text-[12px] font-bold text-gray-900 tabular-nums">
                    {item.visits >= 1000 ? `${(item.visits / 1000).toFixed(1)}k` : item.visits}
                  </span>
                </div>
                <div className="w-full h-1 bg-gray-100 rounded-full overflow-hidden">
                  <div 
                    className="h-full bg-blue-500 rounded-full transition-all duration-1000 ease-out"
                    style={{ width: `${(item.visits / maxVisits) * 100}%` }}
                  />
                </div>
                <div className="mt-1 flex items-center justify-between">
                   <span className="text-[10px] text-gray-400">{totalVisits > 0 ? ((item.visits / totalVisits) * 100).toFixed(1) : 0}% of total</span>
                   {(item as any).continent && (
                     <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500 font-medium">{(item as any).continent}</span>
                   )}
                </div>
              </div>
              ))
            ) : (
              // Continent View
              continentData.map((item) => (
                <div 
                  key={item.continent} 
                  className="flex flex-col p-2.5 rounded-lg hover:bg-gray-50 transition-colors group cursor-pointer border border-transparent hover:border-gray-100"
                >
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-2">
                      <span 
                        className="w-2.5 h-2.5 rounded-full"
                        style={{ backgroundColor: continentColors[item.continent] || '#9CA3AF' }}
                      />
                      <span className="text-[13px] font-medium text-gray-700 group-hover:text-blue-600 transition-colors">{item.continent}</span>
                    </div>
                    <span className="text-[12px] font-bold text-gray-900 tabular-nums">
                      {item.visits >= 1000 ? `${(item.visits / 1000).toFixed(1)}k` : item.visits}
                    </span>
                  </div>
                  <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div 
                      className="h-full rounded-full transition-all duration-1000 ease-out"
                      style={{ 
                        width: `${(item.visits / maxContinentVisits) * 100}%`,
                        backgroundColor: continentColors[item.continent] || '#9CA3AF',
                      }}
                    />
                  </div>
                  <div className="mt-1">
                    <span className="text-[10px] text-gray-400">{totalVisits > 0 ? ((item.visits / totalVisits) * 100).toFixed(1) : 0}% of total</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </ChartContainer>
  );
}

export default memo(TopLocations);
