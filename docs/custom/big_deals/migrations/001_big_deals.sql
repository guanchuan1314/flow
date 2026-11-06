CREATE TABLE custom_big_deals (
    ticket bigint PRIMARY KEY,
    login bigint NOT NULL,
    symbol text NOT NULL,
    lots numeric(18, 4) NOT NULL,
    time timestamp NOT NULL
);
CREATE INDEX ix_custom_big_deals_time ON custom_big_deals (time DESC);
