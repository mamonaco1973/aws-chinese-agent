FROM public.ecr.aws/lambda/microvms:al2023-minimal
# python3 runs the supervisor and the Python session; bash is the second
# session, and jq frames its JSON-lines protocol. numpy, matplotlib and pillow
# are installed here, at image build, because the demo prompt is "draw me
# something": a pip install at request time would take minutes on camera,
# while a package baked into the image costs nothing per launch.
#
# The minimal base has no ps, find, xargs, tar or which; a shell session
# without them sends the model off installing basics before it can start.
# These are small. git is left out on purpose (it pulls in Perl) -- a
# `dnf install -y git` from run_shell takes seconds when a task needs it.
#
# Do NOT add coreutils: the base image ships coreutils-single, the two
# conflict, and dnf (really microdnf) fails the build with an opaque error.
RUN dnf install -y python3 python3-pip jq \
      procps-ng findutils which tar gzip unzip diffutils file hostname \
    && dnf clean all
RUN python3 -m pip install --no-cache-dir numpy matplotlib pillow

# Agg renders to files with no display. MPLCONFIGDIR puts matplotlib's font
# cache on the image disk, so it is built once here instead of on the first
# plot in every MicroVM.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/app/.matplotlib

WORKDIR /app
COPY server.py kernel.py shell.sh ./
EXPOSE 8080 8081
CMD ["python3", "server.py"]
