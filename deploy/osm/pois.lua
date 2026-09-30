-- P14 — osm_pois flex style for osm2pgsql.
--
-- Named features carrying an amenity/shop/tourism/leisure/office/craft
-- tag become searchable rows; everything else is dropped (this is a POI
-- mirror, not a rendering schema). Nodes insert their point; closed
-- ways insert the polygon centroid. The table is created by osm2pgsql
-- itself — it intentionally lives outside db/migrations so re-imports
-- own the whole lifecycle.

local pois = osm2pgsql.define_table({
    name = 'osm_pois',
    ids = { type = 'any', id_column = 'osm_id', type_column = 'osm_type' },
    columns = {
        { column = 'name',            type = 'text' },
        { column = 'name_en',         type = 'text' },
        { column = 'amenity',         type = 'text' },
        { column = 'shop',            type = 'text' },
        { column = 'tourism',         type = 'text' },
        { column = 'leisure',         type = 'text' },
        { column = 'office',          type = 'text' },
        { column = 'craft',           type = 'text' },
        { column = 'addr_housenumber', type = 'text' },
        { column = 'addr_street',     type = 'text' },
        { column = 'addr_district',   type = 'text' },
        { column = 'addr_city',       type = 'text' },
        { column = 'phone',           type = 'text' },
        { column = 'website',         type = 'text' },
        { column = 'opening_hours',   type = 'text' },
        { column = 'geom',            type = 'point', projection = 4326 },
    },
    indexes = {
        { column = 'geom',    method = 'gist' },
        { column = 'amenity', method = 'btree' },
        { column = 'shop',    method = 'btree' },
    },
})

local POI_KEYS = { 'amenity', 'shop', 'tourism', 'leisure', 'office', 'craft' }

local function poi_tagged(tags)
    for _, key in ipairs(POI_KEYS) do
        if tags[key] then
            return true
        end
    end
    return false
end

local function add_poi(tags, geom)
    pois:insert({
        name = tags.name,
        name_en = tags['name:en'],
        amenity = tags.amenity,
        shop = tags.shop,
        tourism = tags.tourism,
        leisure = tags.leisure,
        office = tags.office,
        craft = tags.craft,
        addr_housenumber = tags['addr:housenumber'],
        addr_street = tags['addr:street'],
        addr_district = tags['addr:district'],
        addr_city = tags['addr:city'],
        phone = tags.phone or tags['contact:phone'],
        website = tags.website or tags['contact:website'],
        opening_hours = tags.opening_hours,
        geom = geom,
    })
end

function osm2pgsql.process_node(object)
    local tags = object.tags
    if not tags.name or not poi_tagged(tags) then
        return
    end
    add_poi(tags, object:as_point())
end

function osm2pgsql.process_way(object)
    local tags = object.tags
    if not object.is_closed or not tags.name or not poi_tagged(tags) then
        return
    end
    add_poi(tags, object:as_polygon():centroid())
end
