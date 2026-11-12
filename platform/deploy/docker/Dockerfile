FROM scratch
COPY zgotify /zgotify
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
USER 65532:65532
EXPOSE 8090
ENTRYPOINT ["/zgotify"]
CMD ["-listen", ":8090"]
