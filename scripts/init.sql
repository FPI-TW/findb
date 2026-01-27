-- Database initialization script
-- Creates required schemas and initial data

-- Create raw schema
CREATE SCHEMA IF NOT EXISTS raw;

-- Grant permissions
GRANT ALL ON SCHEMA raw TO findb;
GRANT ALL ON SCHEMA public TO findb;

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
