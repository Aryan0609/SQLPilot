FROM node:22-alpine
WORKDIR /app
ENV API_PROXY_TARGET=http://api:8000
COPY package*.json ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
